from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import get_settings
from app.db.models import Plan, Role
from app.services.support_contacts import (
    active_support_contacts,
    parse_support_contacts,
    support_chat_url,
)
from app.services.users import DEFAULT_SETTINGS, on

# Default order for user menu items (drag-and-drop in web panel edits menu_order)
# guide/faq removed from keyboard — keep texts available via /help if needed
DEFAULT_MENU_ORDER = [
    "shop",
    "services",
    "wallet",
    "support",
    "referral",
    "reseller_apply",
    "miniapp",
]

# Legacy keys stripped from saved menu_order so old installs drop them from the keyboard
REMOVED_MENU_KEYS = frozenset({"guide", "faq", "restart", "help"})


def _t(ui: dict | None, key: str) -> str:
    if ui and key in ui and ui[key]:
        return ui[key]
    return DEFAULT_SETTINGS.get(key, key)


def chunk_buttons(
    buttons: list[InlineKeyboardButton],
    *,
    cols: int = 2,
) -> list[list[InlineKeyboardButton]]:
    """Pack inline buttons into N-column rows."""
    if cols < 1:
        cols = 1
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), cols):
        rows.append(buttons[i : i + cols])
    return rows


def _resolve_ui(ui: dict | None) -> dict | None:
    if ui is not None:
        return ui
    try:
        from app.services.users import current_ui_snapshot

        return current_ui_snapshot()
    except Exception:
        return None


def _menu_layout(ui: dict | None) -> str:
    resolved = _resolve_ui(ui)
    return (_t(resolved, "menu_layout") or "compact").strip()


def layout_rows(
    buttons: list[InlineKeyboardButton],
    ui: dict | None = None,
    *,
    full_width: list[InlineKeyboardButton] | None = None,
) -> list[list[InlineKeyboardButton]]:
    """Apply panel menu_layout: compact = 2-col, classic = 1-col; append full-width rows."""
    layout = _menu_layout(ui)
    if layout == "compact":
        rows = chunk_buttons(buttons, cols=2)
    else:
        rows = [[b] for b in buttons]
    for b in full_width or []:
        rows.append([b])
    return rows


def _menu_order(ui: dict | None) -> list[str]:
    """Active menu keys from menu_order only (no re-inject of removed items)."""
    raw = _t(ui, "menu_order")
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    known = set(DEFAULT_MENU_ORDER)
    ordered = [p for p in parts if p in known and p not in REMOVED_MENU_KEYS]
    # shop always present
    if "shop" not in ordered:
        ordered.insert(0, "shop")
    return ordered


MENU_SHOW_KEYS = (
    "wallet",
    "support",
    "referral",
    "reseller_apply",
    "miniapp",
    "services",
)


def sync_show_flags_for_order(order: list[str]) -> dict[str, str]:
    """Map menu_order → show_* flags (for persistence / legacy readers)."""
    active = set(order)
    return {f"show_{key}": ("1" if key in active else "0") for key in MENU_SHOW_KEYS}


def main_menu(
    role: str,
    *,
    has_services: bool = False,
    ui: dict | None = None,
    as_user: bool = False,
    show_reseller_creds: bool = False,
) -> InlineKeyboardMarkup:
    """
    Legacy/preview inline main menu (also used for admin «پیش‌نمایش»).
    Live home navigation uses ``main_reply_keyboard`` instead.
    """
    if role == Role.ADMIN.value and not as_user:
        return admin_main_menu(ui)

    settings = get_settings()
    buttons: list[InlineKeyboardButton] = []

    for key in _menu_order(ui):
        if key == "shop":
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_shop"), callback_data="shop:list")
            )
        elif key == "services" and has_services:
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_services"), callback_data="svc:list")
            )
        elif key == "wallet":
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_wallet"), callback_data="wallet:home")
            )
        elif key == "support":
            contacts = active_support_contacts(
                parse_support_contacts((ui or {}).get("support_contacts"))
            )
            if len(contacts) == 1:
                url = support_chat_url(contacts[0].get("telegram") or "")
                if url:
                    buttons.append(
                        InlineKeyboardButton(text=_t(ui, "btn_support"), url=url)
                    )
                else:
                    buttons.append(
                        InlineKeyboardButton(
                            text=_t(ui, "btn_support"), callback_data="support:home"
                        )
                    )
            else:
                buttons.append(
                    InlineKeyboardButton(
                        text=_t(ui, "btn_support"), callback_data="support:home"
                    )
                )
        elif key == "guide":
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_guide"), callback_data="help:guide")
            )
        elif key == "faq":
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_faq"), callback_data="help:faq")
            )
        elif key == "referral":
            buttons.append(
                InlineKeyboardButton(text=_t(ui, "btn_referral"), callback_data="ref:home")
            )
        elif key == "reseller_apply" and role == Role.USER.value and not show_reseller_creds:
            buttons.append(
                InlineKeyboardButton(
                    text=_t(ui, "btn_reseller_apply"),
                    callback_data="resapply:home",
                )
            )
        elif key == "miniapp" and settings.miniapp_enabled:
            buttons.append(
                InlineKeyboardButton(
                    text=_t(ui, "btn_miniapp"),
                    web_app=WebAppInfo(url=settings.miniapp_url),
                )
            )

    # compact = pair left-to-right like the web-panel live preview; classic = one per row
    full_width: list[InlineKeyboardButton] = []
    if role == Role.RESELLER.value:
        # Full shop panel — only on the dedicated reseller bot
        full_width.append(
            InlineKeyboardButton(text=_t(ui, "btn_reseller"), callback_data="res:home")
        )
    elif show_reseller_creds:
        # Main bot: credentials / deep-link only — no panel ops here
        full_width.append(
            InlineKeyboardButton(
                text=_t(ui, "btn_reseller_creds"),
                callback_data="res:creds",
            )
        )
    if role == Role.ADMIN.value and as_user:
        full_width.append(
            InlineKeyboardButton(text=_t(ui, "btn_admin"), callback_data="adm:home")
        )
    rows = layout_rows(buttons, ui, full_width=full_width)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Reply-keyboard action keys (text labels resolved from settings)
REPLY_ACTION_HOME = "home"
REPLY_ACTION_BACK = "back"
REPLY_ACTION_SHOP = "shop"
REPLY_ACTION_SERVICES = "services"
REPLY_ACTION_WALLET = "wallet"
REPLY_ACTION_WALLET_TOPUP = "wallet_topup"
REPLY_ACTION_WALLET_TX = "wallet_tx"
REPLY_ACTION_SUPPORT = "support"
REPLY_ACTION_SUPPORT_NEW = "support_new"
REPLY_ACTION_SUPPORT_LIST = "support_list"
REPLY_ACTION_REFERRAL = "referral"
REPLY_ACTION_RESELLER_APPLY = "reseller_apply"
REPLY_ACTION_RESELLER = "reseller"
REPLY_ACTION_CREDS = "reseller_creds"
REPLY_ACTION_ADMIN = "admin"
REPLY_ACTION_ADMIN_DASH = "adm_dash"
REPLY_ACTION_ADMIN_ORDERS = "adm_orders"
REPLY_ACTION_ADMIN_PAYMENTS = "adm_payments"
REPLY_ACTION_ADMIN_TICKETS = "adm_tickets"
REPLY_ACTION_ADMIN_PLANS = "adm_plans"
REPLY_ACTION_ADMIN_PG = "adm_pg"
REPLY_ACTION_ADMIN_PREVIEW = "adm_preview"
REPLY_ACTION_RES_PREVIEW = "res_preview"
REPLY_ACTION_ADMIN_USERS = "adm_users"
REPLY_ACTION_ADMIN_SETTINGS = "adm_settings"
REPLY_ACTION_ADMIN_BROADCAST = "adm_broadcast"
REPLY_ACTION_ADMIN_RESELLERS = "adm_resellers"
REPLY_ACTION_ADMIN_BACKUP = "adm_backup"
REPLY_ACTION_PAY_WALLET = "pay_wallet"
REPLY_ACTION_PAY_CARD = "pay_card"
REPLY_ACTION_PAY_GATEWAY = "pay_gateway"
REPLY_ACTION_PAY_CRYPTO = "pay_crypto"
REPLY_ACTION_PAY_STARS = "pay_stars"
REPLY_ACTION_PAY_DISCOUNT = "pay_discount"
REPLY_ACTION_TOPUP_CARD = "topup_card"
REPLY_ACTION_TOPUP_GATEWAY = "topup_gateway"
REPLY_ACTION_TOPUP_CRYPTO = "topup_crypto"
REPLY_ACTION_PG_STATS = "pg_stats"
REPLY_ACTION_PG_USERS = "pg_users"
REPLY_ACTION_PG_CREATE = "pg_create"
REPLY_ACTION_PG_SEARCH = "pg_search"
REPLY_ACTION_PG_NODES = "pg_nodes"
REPLY_ACTION_PG_GROUP = "pg_group"
REPLY_ACTION_PG_TEMPLATE = "pg_template"
REPLY_ACTION_ADM_USERS_LIST = "adm_users_list"
REPLY_ACTION_ADM_USERS_SEARCH = "adm_users_search"
REPLY_ACTION_ADM_USERS_WEB = "adm_users_web"
REPLY_ACTION_ADM_RES_LIST = "adm_res_list"
REPLY_ACTION_ADM_RES_APPS = "adm_res_apps"
REPLY_ACTION_ADM_RES_ADD = "adm_res_add"

BTN_BACK = "⬅️ بازگشت"


def _home_label(ui: dict | None = None) -> str:
    return (_t(ui, "btn_menu_home") or "🏠 منوی اصلی").strip() or "🏠 منوی اصلی"


def _back_label(ui: dict | None = None) -> str:
    return (_t(ui, "btn_back") or BTN_BACK).strip() or BTN_BACK


def _reply_markup(
    rows: list[list[KeyboardButton]],
    *,
    placeholder: str = "از منوی پایین انتخاب کنید…",
) -> ReplyKeyboardMarkup:
    """Standard reply keyboard — not persistent (no side menu icon)."""
    return ReplyKeyboardMarkup(
        keyboard=rows or [[KeyboardButton(text=_home_label())]],
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
) -> list[tuple[str, str]]:
    """Ordered (action_key, button_text) for the customer/reseller reply keyboard."""
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
        elif key == "referral":
            entries.append((REPLY_ACTION_REFERRAL, _t(ui, "btn_referral")))
        elif key == "reseller_apply" and role == Role.USER.value and not show_reseller_creds:
            entries.append((REPLY_ACTION_RESELLER_APPLY, _t(ui, "btn_reseller_apply")))
        elif key == "miniapp":
            # WebApp requires inline buttons — shown separately under welcome
            continue
        elif key == "services" and not has_services:
            continue
    if role == Role.RESELLER.value:
        entries.append((REPLY_ACTION_RESELLER, _t(ui, "btn_reseller")))
    elif show_reseller_creds:
        entries.append((REPLY_ACTION_CREDS, _t(ui, "btn_reseller_creds")))
    if role == Role.ADMIN.value:
        entries.append((REPLY_ACTION_ADMIN, _t(ui, "btn_admin")))
    return entries


def _reply_admin_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_ADMIN_DASH, "📊 داشبورد"),
        (REPLY_ACTION_ADMIN_ORDERS, _t(ui, "btn_adm_orders")),
        (REPLY_ACTION_ADMIN_PAYMENTS, _t(ui, "btn_adm_payments")),
        (REPLY_ACTION_ADMIN_TICKETS, _t(ui, "btn_adm_tickets")),
        (REPLY_ACTION_ADMIN_PLANS, _t(ui, "btn_adm_plans")),
        (REPLY_ACTION_ADMIN_USERS, _t(ui, "btn_adm_users")),
        (REPLY_ACTION_ADMIN_RESELLERS, "🤝 نمایندگان"),
        (REPLY_ACTION_ADMIN_PG, _t(ui, "btn_adm_pg")),
        (REPLY_ACTION_ADMIN_SETTINGS, _t(ui, "btn_adm_settings")),
        (REPLY_ACTION_ADMIN_BROADCAST, _t(ui, "btn_adm_broadcast")),
        (REPLY_ACTION_ADMIN_BACKUP, "💾 بکاپ / ریستور"),
        (REPLY_ACTION_ADMIN_PREVIEW, _t(ui, "btn_adm_preview")),
    ]


def _pg_submenu_entries(ui: dict | None = None, *, actor: dict | None = None) -> list[tuple[str, str]]:
    """PG reply submenu — feature set from authz (identical gates as web panel)."""
    _ = ui
    from app.services.authz import bot_pg_feature_entries

    return bot_pg_feature_entries(actor)


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
    # Labels MUST match admin_settings.SECTIONS[*]["title"]
    return [
        ("adm_st_shop", "فروشگاه و متون"),
        ("adm_st_menu", "کیبورد اصلی"),
        ("adm_st_pay", "پرداخت"),
        ("adm_st_support", "پشتیبان‌ها"),
        ("adm_st_service", "سرویس و دسترسی"),
        ("adm_st_notify", "اعلان‌ها"),
    ]


def _admin_backup_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        ("backup_create", "🆕 ساخت بکاپ کامل"),
        ("backup_create_noenv", "🆕 بکاپ بدون .env"),
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
    return [
        ("adm_plan_add", "➕ پلن جدید"),
        ("adm_plan_custom", "پلن دلخواه"),
        ("adm_plan_trial", "پلن تست"),
    ]


def _reseller_plans_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    return [
        ("res_plan_add", "➕ پلن جدید"),
    ]


def _reseller_settings_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    _ = ui
    # Labels MUST match reseller_settings.SECTIONS[*]["title"] / HUB_ORDER
    return [
        ("res_st_shop", "فروشگاه و متون"),
        ("res_st_menu", "کیبورد اصلی"),
        ("res_st_pay", "پرداخت"),
        ("res_st_support", "پشتیبان‌ها"),
        ("res_st_access", "دسترسی و QR"),
        ("res_st_bot", "ربات اختصاصی"),
        ("res_st_notify", "نوتیفیکیشن‌ها"),
    ]


def _wallet_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_WALLET_TOPUP, "🟢➕ شارژ کیف پول"),
        (REPLY_ACTION_WALLET_TX, "🟡📜 تراکنش‌ها"),
    ]


def _support_submenu_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_SUPPORT_NEW, "🟣✉️ تیکت جدید"),
        (REPLY_ACTION_SUPPORT_LIST, "📋 تیکت‌های من"),
    ]


def _reseller_submenu_entries(profile=None) -> list[tuple[str, str]]:
    from app.services.authz import can_shop_profile

    # Fail closed: without a live profile show nothing (matches reply_action_map)
    if profile is None:
        return []
    entries: list[tuple[str, str]] = []
    if can_shop_profile(profile, "dashboard"):
        entries.append(("res_dash", "🏠 خانه نماینده"))
        entries.append(("res_users", "👥 مشتریان من"))
    # PAYG billing wallet — only when mode is payg
    try:
        from app.services.billing import is_payg

        if is_payg(profile):
            entries.append(("res_billing", "💰 کیف پول"))
    except Exception:
        pass
    if can_shop_profile(profile, "stats"):
        entries.append(("res_stats", "📊 آمار و کمیسیون"))
    if can_shop_profile(profile, "plans"):
        entries.append(("res_plans", "💎 پلن‌های فروش"))
    if can_shop_profile(profile, "orders"):
        entries.append(("res_orders", "🛒 سفارش‌های مشتریان"))
    if can_shop_profile(profile, "payments"):
        entries.append(("res_payments", "🧾 رسیدهای در انتظار"))
    if can_shop_profile(profile, "tickets"):
        entries.append(("res_tickets", "🎫 تیکت‌های مشتریان"))
    if can_shop_profile(profile, "shop_settings"):
        entries.append(("res_settings", "⚙️ تنظیمات فروشگاه"))
    # Shop owner is admin of their bot — preview customer keyboard
    entries.append((REPLY_ACTION_RES_PREVIEW, "👁 پیش‌نمایش منوی کاربر"))
    return entries


def reseller_hub_main_keyboard(profile=None, ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Primary keyboard for shop owner/staff on their dedicated bot (like admin hub)."""
    entries = _reseller_submenu_entries(profile)
    rows = _pack_reply_rows(entries, ui, footer=[_home_label(ui)])
    return _reply_markup(
        rows or [[KeyboardButton(text=_home_label(ui))]],
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
    if on(_t(ui, "pay_crypto_enabled")):
        entries.append((REPLY_ACTION_TOPUP_CRYPTO, _t(ui, "btn_pay_crypto")))
    return entries


def _pack_reply_rows(
    entries: list[tuple[str, str]],
    ui: dict | None,
    *,
    footer: list[str] | None = None,
    footer_row: list[str] | None = None,
) -> list[list[KeyboardButton]]:
    layout = _menu_layout(ui)
    texts = [t for _, t in entries if (t or "").strip()]
    rows: list[list[KeyboardButton]] = []
    if layout == "compact":
        for i in range(0, len(texts), 2):
            chunk = texts[i : i + 2]
            rows.append([KeyboardButton(text=x) for x in chunk])
    else:
        for t in texts:
            rows.append([KeyboardButton(text=t)])
    if footer_row:
        row = [KeyboardButton(text=t) for t in footer_row if (t or "").strip()]
        if row:
            rows.append(row)
    for label in footer or []:
        if label:
            rows.append([KeyboardButton(text=label)])
    return rows


def _submenu_footer(ui: dict | None = None) -> list[str]:
    """Back (one level) + Main menu — always on submenu keyboards."""
    return [_back_label(ui), _home_label(ui)]


def main_reply_keyboard(
    role: str,
    *,
    has_services: bool = False,
    ui: dict | None = None,
    as_user: bool = False,
    show_reseller_creds: bool = False,
) -> ReplyKeyboardMarkup:
    """Primary navigation reply keyboard (level 0)."""
    home_label = _home_label(ui)
    if role == Role.ADMIN.value and not as_user:
        entries = _reply_admin_entries(ui)
        rows = _pack_reply_rows(entries, ui, footer=[home_label])
    else:
        # Preview / customer surface — never append staff-only buttons
        map_role = Role.USER.value if as_user else role
        entries = _reply_user_entries(
            map_role,
            has_services=has_services,
            ui=ui,
            show_reseller_creds=False if as_user else show_reseller_creds,
        )
        rows = _pack_reply_rows(entries, ui, footer=[home_label])
    return _reply_markup(rows, placeholder="از منوی پایین انتخاب کنید…")


def admin_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Admin panel as an explicit submenu (with back + home)."""
    rows = _pack_reply_rows(
        _reply_admin_entries(ui),
        ui,
        footer_row=_submenu_footer(ui),
    )
    return _reply_markup(rows, placeholder="پنل ادمین…")


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


def reseller_reply_keyboard(profile=None, ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Reply-keyboard mirror of reseller panel sections."""
    entries = _reseller_submenu_entries(profile)
    rows = _pack_reply_rows(entries, ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows or [[KeyboardButton(text=_home_label(ui))]], placeholder="پنل نماینده…")


def pay_reply_keyboard(order_id: int, ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Order payment methods on reply keyboard (order_id kept in FSM)."""
    _ = order_id  # identity reserved for callers / future per-order labels
    rows = _pack_reply_rows(_pay_method_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="روش پرداخت را انتخاب کنید…")


def topup_pay_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_topup_method_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="روش شارژ را انتخاب کنید…")


def pg_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_pg_submenu_entries(ui), ui, footer_row=_submenu_footer(ui))
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


def admin_plans_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(
        _admin_plans_submenu_entries(ui), ui, footer_row=_submenu_footer(ui)
    )
    return _reply_markup(rows, placeholder="پلن‌های فروش…")


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


def reseller_plans_list_keyboard(plans: list) -> InlineKeyboardMarkup:
    """Dynamic plan rows only (static chrome on reseller_plans_reply_keyboard)."""
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans[:20]:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        price = getattr(p, "price", 0) or 0
        label = f"{flag} {name} · {price:,}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"res:plan:view:{p.id}")]
        )
    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text="پلنی نیست — از کیبورد «پلن جدید»",
                    callback_data="res:plans",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        for key, text in _reply_admin_entries(ui):
            mapping[(text or "").strip()] = key
    else:
        # On reseller bots, never expose platform-admin entry even if role string is admin
        map_role = Role.USER.value if (is_reseller_bot and role == Role.ADMIN.value) else role
        for key, text in _reply_user_entries(
            map_role,
            has_services=has_services,
            ui=ui,
            show_reseller_creds=show_reseller_creds,
        ):
            mapping[(text or "").strip()] = key
        # Preview escape on main bot only
        if role == Role.ADMIN.value and as_user and not is_reseller_bot:
            mapping[(_t(ui, "btn_admin") or "").strip()] = REPLY_ACTION_ADMIN

    if include_submenus:
        # Customer surfaces (wallet/support/pay) — everyone except pure admin hub
        if not platform_admin:
            for key, text in _wallet_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _support_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _pay_method_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _topup_method_entries(ui):
                mapping[(text or "").strip()] = key

        if platform_admin:
            for key, text in _wallet_submenu_entries(ui):
                mapping[(text or "").strip()] = key
            for key, text in _support_submenu_entries(ui):
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
            # Hub labels win over any accidental submenu collisions
            for key, text in _reply_admin_entries(ui):
                mapping[(text or "").strip()] = key

        # Shop chrome labels (custom/wholesale) — always registered; keyboard
        # only shows them when enabled.
        for key, text in _shop_submenu_entries(
            ui, custom_enabled=True, wholesale_enabled=True
        ):
            mapping.setdefault((text or "").strip(), key)

        if reseller_actor:
            from app.services.authz import can_shop_profile

            # Fail closed: without a live profile, register no reseller panel labels
            if profile is not None:
                for key, text in _reseller_submenu_entries(profile):
                    mapping[(text or "").strip()] = key
                if can_shop_profile(profile, "shop_settings"):
                    for key, text in _reseller_settings_submenu_entries(ui):
                        mapping[(text or "").strip()] = key
                if can_shop_profile(profile, "plans"):
                    for key, text in _reseller_plans_submenu_entries(ui):
                        mapping[(text or "").strip()] = key

    return {k: v for k, v in mapping.items() if k}


def miniapp_inline_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup | None:
    """WebApp can only live on inline keyboards. Platform bot only."""
    from app.services.users import current_shop_reseller_id

    # Shop bots never advertise the platform miniapp (HMAC uses main bot token)
    if current_shop_reseller_id():
        return None
    settings = get_settings()
    if not settings.miniapp_enabled:
        return None
    if "miniapp" not in _menu_order(ui):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_miniapp"),
                    web_app=WebAppInfo(url=settings.miniapp_url),
                )
            ]
        ]
    )


def admin_main_menu(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — live admin home uses admin_reply_keyboard / main_reply_keyboard."""
    _ = ui
    return InlineKeyboardMarkup(inline_keyboard=[])


REPLY_ACTION_SHOP_CUSTOM = "shop_custom"
REPLY_ACTION_SHOP_WHOLESALE = "shop_wholesale"


def _shop_submenu_entries(
    ui: dict | None = None,
    *,
    custom_enabled: bool = False,
    wholesale_enabled: bool = False,
) -> list[tuple[str, str]]:
    """Static shop chrome (custom/wholesale). Plan names stay inline."""
    entries: list[tuple[str, str]] = []
    if custom_enabled:
        entries.append((REPLY_ACTION_SHOP_CUSTOM, "✨ پلن دلخواه"))
    if wholesale_enabled:
        entries.append(
            (REPLY_ACTION_SHOP_WHOLESALE, _t(ui, "btn_wholesale") or "📦 فروش عمده")
        )
    return entries


def shop_reply_keyboard(
    ui: dict | None = None,
    *,
    custom_enabled: bool = False,
    wholesale_enabled: bool = False,
) -> ReplyKeyboardMarkup:
    """Shop chrome on reply KB; plan picks are inline under the message."""
    entries = _shop_submenu_entries(
        ui, custom_enabled=custom_enabled, wholesale_enabled=wholesale_enabled
    )
    rows = _pack_reply_rows(entries, ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="فروشگاه — پلن را از زیر پیام انتخاب کنید…")


def plans_keyboard(
    plans: list[Plan],
    ui: dict | None = None,
    *,
    custom_enabled: bool = False,
    wholesale_enabled: bool = False,
) -> InlineKeyboardMarkup:
    """Plan name rows only — custom/wholesale/back live on shop_reply_keyboard."""
    _ = custom_enabled, wholesale_enabled, ui  # kept for call-site compat
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🎁' if p.is_trial else '💎'} {p.name} — {p.price:,} ت".replace(",", "٬"),
                callback_data=f"shop:plan:{p.id}",
            )
        ]
        for p in plans
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text="پلنی نیست", callback_data="shop:list")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wholesale_plans_keyboard(
    plans: list[Plan],
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Wholesale plan names only (back/chrome on reply KB)."""
    _ = ui
    rows = [
        [
            InlineKeyboardButton(
                text=f"💎 {p.name} — {p.price:,} ت".replace(",", "٬"),
                callback_data=f"shop:wholesale:plan:{p.id}",
            )
        ]
        for p in plans
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text="پلنی نیست", callback_data="shop:wholesale")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wholesale_qty_keyboard(
    qty: int,
    ui: dict | None = None,
    *,
    plan_id: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="➖", callback_data="shop:wholesale:qty:-"),
            InlineKeyboardButton(text=f"{qty} عدد", callback_data="shop:wholesale:noop"),
            InlineKeyboardButton(text="➕", callback_data="shop:wholesale:qty:+"),
        ],
        [InlineKeyboardButton(text="✏️ ورود دستی تعداد", callback_data="shop:wholesale:qty:input")],
        [InlineKeyboardButton(text="ادامه ← تأیید", callback_data="shop:wholesale:confirm")],
        [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="shop:wholesale")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wholesale_confirm_keyboard(
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأیید و پرداخت", callback_data="shop:wholesale:buy")],
            [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="shop:wholesale:qty")],
        ]
    )


def custom_gb_keyboard(
    gb: int,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="➖", callback_data="shop:custom:gb:-"),
            InlineKeyboardButton(text=f"{gb} گیگ", callback_data="shop:custom:noop"),
            InlineKeyboardButton(text="➕", callback_data="shop:custom:gb:+"),
        ],
        [InlineKeyboardButton(text="✏️ ورود دستی حجم", callback_data="shop:custom:gb:input")],
        [InlineKeyboardButton(text="ادامه ← روزها", callback_data="shop:custom:gb:next")],
        [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="shop:list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_days_keyboard(
    days: int,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data="shop:custom:days:-"),
                InlineKeyboardButton(text=f"{days} روز", callback_data="shop:custom:noop"),
                InlineKeyboardButton(text="➕", callback_data="shop:custom:days:+"),
            ],
            [InlineKeyboardButton(text="✏️ ورود دستی روز", callback_data="shop:custom:days:input")],
            [InlineKeyboardButton(text="✅ مشاهده قیمت و تأیید", callback_data="shop:custom:confirm")],
            [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="shop:custom")],
        ]
    )


def custom_confirm_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    _ = ui
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢✅ ادامه خرید", callback_data="shop:custom:buy")],
            [InlineKeyboardButton(text="✏️ تغییر روز", callback_data="shop:custom:gb:next")],
            [InlineKeyboardButton(text="✏️ تغییر حجم", callback_data="shop:custom")],
        ]
    )


def plan_actions(plan_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """One-shot buy confirm under the plan message (no back chrome)."""
    _ = ui
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢✅ ادامه خرید", callback_data=f"shop:buy:{plan_id}")],
        ]
    )


def any_checkout_method_enabled(ui: dict | None = None) -> bool:
    """True if at least one real checkout method (not just discount) is on."""
    return any(
        on(_t(ui, k))
        for k in (
            "pay_wallet_enabled",
            "pay_card_enabled",
            "pay_gateway_enabled",
            "pay_crypto_enabled",
            "pay_stars_enabled",
        )
    )


def pay_methods(order_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if on(_t(ui, "pay_wallet_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_wallet"),
                    callback_data=f"pay:wallet:{order_id}",
                )
            ]
        )
    if on(_t(ui, "pay_card_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_card"),
                    callback_data=f"pay:card:{order_id}",
                )
            ]
        )
    if on(_t(ui, "pay_gateway_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_gateway"),
                    callback_data=f"pay:gateway:{order_id}",
                )
            ]
        )
    if on(_t(ui, "pay_crypto_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_crypto"),
                    callback_data=f"pay:crypto:{order_id}",
                )
            ]
        )
    if on(_t(ui, "pay_stars_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_stars"),
                    callback_data=f"pay:stars:{order_id}",
                )
            ]
        )
    if on(_t(ui, "pay_discount_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_discount"),
                    callback_data=f"pay:discount:{order_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_t(ui, "btn_cancel"),
                callback_data="menu:home",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_pay_methods(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Payment methods for wallet top-up (no wallet method). Amount lives in FSM."""
    rows: list[list[InlineKeyboardButton]] = []
    if on(_t(ui, "pay_card_enabled")):
        rows.append(
            [InlineKeyboardButton(text=_t(ui, "btn_pay_card"), callback_data="wtop:card")]
        )
    if on(_t(ui, "pay_gateway_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_gateway"),
                    callback_data="wtop:gateway",
                )
            ]
        )
    if on(_t(ui, "pay_crypto_enabled")):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(ui, "btn_pay_crypto"),
                    callback_data="wtop:crypto",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=_t(ui, "btn_cancel"), callback_data="wallet:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_keyboard(services: list, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Entity names only — actions/back live on reply keyboard."""
    _ = ui
    rows = [
        [
            InlineKeyboardButton(
                text=f"🔹 {s.pg_username}",
                callback_data=f"svc:view:{s.id}",
            )
        ]
        for s in services
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text="سرویسی نیست", callback_data="svc:list")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


REPLY_ACTION_SVC_LINK = "svc_link"
REPLY_ACTION_SVC_RENEW = "svc_renew"
REPLY_ACTION_SVC_REFRESH = "svc_refresh"


def _service_action_entries(ui: dict | None = None) -> list[tuple[str, str]]:
    return [
        (REPLY_ACTION_SVC_LINK, _t(ui, "btn_sub_link")),
        (REPLY_ACTION_SVC_RENEW, _t(ui, "btn_renew")),
        (REPLY_ACTION_SVC_REFRESH, "♻️ رفرش وضعیت"),
    ]


def service_actions_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    rows = _pack_reply_rows(_service_action_entries(ui), ui, footer_row=_submenu_footer(ui))
    return _reply_markup(rows, placeholder="عملیات سرویس…")


def service_actions(service_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — prefer service_actions_reply_keyboard."""
    _ = service_id, ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def wallet_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy inline wallet hub — prefer wallet_reply_keyboard (3.6+)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="menu:home")],
        ]
    )


def support_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy inline support hub — prefer support_reply_keyboard (3.6+)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(ui, "btn_back"), callback_data="menu:home")],
        ]
    )


def support_contacts_keyboard(contacts: list[dict], ui: dict | None = None) -> InlineKeyboardMarkup:
    """URL contact rows only — ticket/back live on support_reply_keyboard."""
    _ = ui
    rows: list[list[InlineKeyboardButton]] = []
    for c in contacts:
        url = support_chat_url(c.get("telegram") or "")
        title = str(c.get("title") or "پشتیبان")[:64]
        if url:
            rows.append([InlineKeyboardButton(text=f"💬 {title}", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_home(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy inline admin hub — prefer admin_reply_keyboard (3.6+).

    Kept for rare edit_text fallbacks; callers should migrate to reply KB.
    """
    back = InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="menu:home")
    return InlineKeyboardMarkup(inline_keyboard=[[back]])


def admin_plans_list_keyboard(plans: list) -> InlineKeyboardMarkup:
    """Dynamic plan rows only (static chrome lives on admin_plans_reply_keyboard)."""
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans[:12]:
        warn = ""
        if not getattr(p, "pg_template_id", None) and not (getattr(p, "pg_group_ids", None) or "").strip():
            warn = " ⚠️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if p.is_active else '⏸'} #{p.id} {p.name}{warn}"[:60],
                    callback_data=f"adm:plan:view:{p.id}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="پلنی نیست — از کیبورد «پلن جدید»", callback_data="adm:plans")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def backup_files_keyboard(backups: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Dynamic backup file rows only (static actions on admin_backup_reply_keyboard)."""
    rows: list[list[InlineKeyboardButton]] = []
    for b in (backups or [])[:8]:
        label = f"📦 {b.get('id', '')[:18]} · {b.get('size_human')}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"adm:backup:item:{b['id']}")]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="بکاپی نیست — از کیبورد بسازید", callback_data="adm:backup")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_users_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy — prefer admin_users_reply_keyboard."""
    back = InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:home")
    return InlineKeyboardMarkup(inline_keyboard=[[back]])


def admin_users_list_keyboard(
    *,
    page: int,
    has_prev: bool,
    has_next: bool,
    rows: list[list[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    """Entity names only (+ page nav). Static chrome on admin_users_reply_keyboard."""
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"adm:users:list:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"adm:users:list:{page + 1}"))
    # Flatten single-button rows then pack into 2 columns
    flat = [btn for row in rows for btn in row]
    kb_rows = chunk_buttons(flat, cols=2)
    if nav:
        kb_rows.append(nav)
    if not kb_rows:
        kb_rows = [[InlineKeyboardButton(text="کاربری نیست", callback_data="adm:users:list:0")]]
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def admin_resellers_menu(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — prefer admin_resellers_reply_keyboard."""
    _ = ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def admin_resellers_list_keyboard(
    *,
    page: int,
    has_prev: bool,
    has_next: bool,
    rows: list[list[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    """Entity names (+ optional page nav). Static chrome lives on reply KB."""
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"adm:resellers:list:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"adm:resellers:list:{page + 1}"))
    flat = [btn for row in rows for btn in row]
    kb_rows = chunk_buttons(flat, cols=2)
    if nav:
        kb_rows.append(nav)
    if not kb_rows:
        kb_rows = [[InlineKeyboardButton(text="نماینده‌ای نیست", callback_data="adm:resellers:list:0")]]
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def admin_user_actions(
    user_id: int,
    *,
    is_blocked: bool,
    role: str | None = None,
    confirm_delete: bool = False,
) -> InlineKeyboardMarkup:
    block_label = "🔓 رفع مسدودی" if is_blocked else "🚫 مسدود کردن"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=block_label, callback_data=f"adm:users:block:{user_id}")],
    ]
    if role == "reseller":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🤝 حذف نمایندگی",
                    callback_data=f"adm:users:unres:{user_id}",
                )
            ]
        )
    if confirm_delete:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚠️ تأیید حذف کامل کاربر",
                    callback_data=f"adm:users:del:{user_id}",
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"adm:users:view:{user_id}")]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 حذف کامل کاربر",
                    callback_data=f"adm:users:delask:{user_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Review action labels (order / payment / reseller-app) — reply keyboard
REPLY_ACTION_REV_OK = "rev_ok"
REPLY_ACTION_REV_NO = "rev_no"
BTN_REV_OK = "🟢✅ تأیید"
BTN_REV_NO = "🔴❌ رد"


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


def order_review(order_id: int) -> InlineKeyboardMarkup:
    """Message-scoped approve/reject (notifications). No nav chrome."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢✅ تأیید سفارش", callback_data=f"ordrev:ok:{order_id}"),
                InlineKeyboardButton(text="🔴❌ رد", callback_data=f"ordrev:no:{order_id}"),
            ],
        ]
    )


def payment_review(payment_id: int) -> InlineKeyboardMarkup:
    """Message-scoped approve/reject (notifications). No nav chrome."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢✅ تأیید دستی", callback_data=f"payrev:ok:{payment_id}"),
                InlineKeyboardButton(text="🔴❌ رد", callback_data=f"payrev:no:{payment_id}"),
            ]
        ]
    )


def reseller_app_review(app_id: int) -> InlineKeyboardMarkup:
    """Message-scoped approve/reject (notifications). No nav chrome."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢✅ تأیید", callback_data=f"adm:resapp:ok:{app_id}"),
                InlineKeyboardButton(text="🔴❌ رد", callback_data=f"adm:resapp:no:{app_id}"),
            ],
        ]
    )


def pg_admin_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy — prefer pg_reply_keyboard."""
    _ = ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def reseller_home(profile=None, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — navigation is on reseller_reply_keyboard."""
    _ = profile, ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def back_home(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — use main reply keyboard / Back+Home footer instead of inline back."""
    _ = ui
    return InlineKeyboardMarkup(inline_keyboard=[])


BTN_CANCEL = "انصراف"
BTN_RESTART = "🏠 شروع مجدد"  # legacy alias only — no longer shown on cancel KB


def cancel_reply(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Thin cancel keyboard during text input — does not include home/restart."""
    label = (_t(ui, "btn_cancel") or "").strip()
    cancel_label = BTN_CANCEL
    if label and "انصراف" in label and label != BTN_CANCEL:
        cancel_label = BTN_CANCEL
    return _reply_markup(
        [[KeyboardButton(text=cancel_label)]],
        placeholder="مقدار را بفرستید یا انصراف بزنید…",
    )


def persistent_reply_keyboard(ui: dict | None = None) -> ReplyKeyboardMarkup:
    """Fallback reply keyboard (home only) when role context is unavailable."""
    home = _home_label(ui)
    return _reply_markup([[KeyboardButton(text=home)]])


def is_cancel_text(text: str | None) -> bool:
    t = (text or "").strip()
    return t in {BTN_CANCEL, "لغو", "cancel", "/cancel"} or t.endswith("انصراف")


def is_home_text(text: str | None, ui: dict | None = None) -> bool:
    """True for main-menu / legacy restart labels."""
    t = (text or "").strip()
    if not t:
        return False
    home = _home_label(ui)
    if t == home:
        return True
    return t in {BTN_RESTART, "شروع مجدد", "restart", "🏠 منوی اصلی"} or t.endswith(
        "شروع مجدد"
    ) or t.endswith("منوی اصلی")


def is_restart_text(text: str | None) -> bool:
    """Backward-compat alias — treats home + legacy restart as home navigation."""
    return is_home_text(text)
