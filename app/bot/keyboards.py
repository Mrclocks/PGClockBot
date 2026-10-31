from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot.button_sources import is_recording, note_button_source
from app.bot.list_pages import page_of, pager_row
from app.services.catalog_pricing import sale_price
from app.config import get_settings
from app.db.models import Plan, Role
from app.services.support_contacts import (
    active_support_contacts,
    parse_support_contacts,
    support_chat_url,
)
from app.services.users import DEFAULT_SETTINGS, on
from app.services.connection_status_texts import CONNECTION_STATUS_MENU_KEY
from app.services import bot_feedback_texts as feedback

# Default order for user menu items (drag-and-drop in web panel edits menu_order)
# guide/faq removed from keyboard — keep texts available via /help if needed.
# Connection «آموزش‌ها» lives on the service card (not the main ReplyKeyboard).
DEFAULT_MENU_ORDER = [
    "shop",
    "services",
    CONNECTION_STATUS_MENU_KEY,
    "wallet",
    "support",
    "loyalty",
    "reseller_apply",
]
# Items active on a fresh install. The connection status button stays in the catalog (the web menu
# editor offers it from «مخزن») but is off until an admin adds it.
DEFAULT_ACTIVE_MENU_ORDER = [key for key in DEFAULT_MENU_ORDER if key != CONNECTION_STATUS_MENU_KEY]

# Legacy keys stripped from saved menu_order so old installs drop them from the keyboard.
# miniapp: MenuButton «Open» only — never ReplyKeyboard / menu DnD / welcome inline.
REMOVED_MENU_KEYS = frozenset({"guide", "faq", "restart", "help", "referral", "miniapp"})


def _t(ui: dict | None, key: str) -> str:
    if ui and key in ui and ui[key]:
        raw = ui[key]
        from app.services.rich_text import button_display_text, is_button_label_key

        if is_button_label_key(key):
            return button_display_text(raw)
        return raw
    return DEFAULT_SETTINGS.get(key, key)


def _label_or(ui: dict | None, key: str, fallback: str) -> str:
    """Settings label when present; never fall back to the raw setting key name."""
    if ui and key in ui and str(ui.get(key) or "").strip():
        return _t(ui, key)
    default = DEFAULT_SETTINGS.get(key)
    if default:
        from app.services.rich_text import button_display_text, is_button_label_key

        if is_button_label_key(key):
            return button_display_text(default)
        return str(default)
    return fallback


def normalize_reply_label(text: str | None) -> str:
    """Strip leading/trailing symbol runs so reply matching survives icon leftovers."""
    import re

    s = re.sub(r"\s+", " ", (text or "").strip())
    # Keep Persian/Arabic letters, Latin, digits; drop ornamental symbols at ends.
    s = re.sub(r"^[^0-9A-Za-z\u0600-\u06FF]+", "", s)
    s = re.sub(r"[^0-9A-Za-z\u0600-\u06FF]+$", "", s)
    return s.strip()


def register_reply_label(mapping: dict, label: str | None, action: str) -> None:
    """Register both raw and normalized label keys for robust reply matching."""
    raw = (label or "").strip()
    if not raw:
        return
    mapping[raw] = action
    norm = normalize_reply_label(raw)
    if norm and norm not in mapping:
        mapping[norm] = action


def resolve_reply_mapping(mapping: dict, text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in mapping:
        return mapping[raw]
    norm = normalize_reply_label(raw)
    if norm and norm in mapping:
        return mapping[norm]
    # Survive leftover leading icons on either the pressed label or the map key.
    if norm:
        for key, action in mapping.items():
            if normalize_reply_label(key) == norm:
                return action
    return None


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


LIST_BUTTON_CAP = 40
SHORT_LABEL_MAX = 20
LONG_LIST_MIN_ITEMS = 7


def list_overflow_line(total: int, shown: int = LIST_BUTTON_CAP) -> str:
    """Message-text note for a list truncated to ``shown`` buttons (empty when nothing is hidden)."""
    hidden = max(0, int(total) - int(shown))
    return f"\n… و {hidden} مورد دیگر" if hidden else ""


def _pack_by_label_width(
    buttons: list[InlineKeyboardButton],
) -> list[list[InlineKeyboardButton]]:
    """Pair consecutive short labels; a long label takes a full row."""
    rows: list[list[InlineKeyboardButton]] = []
    pending: list[InlineKeyboardButton] = []
    for button in buttons:
        if len(button.text) > SHORT_LABEL_MAX:
            rows.extend(chunk_buttons(pending, cols=2))
            pending = []
            rows.append([button])
        else:
            pending.append(button)
    rows.extend(chunk_buttons(pending, cols=2))
    return rows


def pack_long_list(
    buttons: list[InlineKeyboardButton],
) -> list[list[InlineKeyboardButton]]:
    """Rows for a list of fixed menu entries: a short list stays one per row, a long one goes two-column.

    Only for entries whose label does not depend on stored values, so a button never changes width
    between renders. Back / search rows are added by the caller and stay full width.
    """
    if len(buttons) < LONG_LIST_MIN_ITEMS:
        return [[button] for button in buttons]
    return _pack_by_label_width(buttons)


def pack_detail_rows(
    rows: list[list[InlineKeyboardButton]],
    *,
    pair_prefixes: tuple[str, str] | None = None,
) -> list[list[InlineKeyboardButton]]:
    """Two-column detail actions: last row (back) stays alone.

    With ``pair_prefixes`` (toggle, delete callback prefixes) both buttons share one row
    placed just above the footer; if either is missing they stay in the normal flow.
    """
    footer = rows[-1]
    actions = [row[0] for row in rows[:-1]]
    pair_row: list[InlineKeyboardButton] = []
    if pair_prefixes:
        picked = [
            next((b for b in actions if (b.callback_data or "").startswith(prefix)), None)
            for prefix in pair_prefixes
        ]
        if all(picked):
            pair_row = [b for b in picked if b is not None]
            actions = [b for b in actions if all(b is not p for p in pair_row)]
    packed = chunk_buttons(actions, cols=2)
    if pair_row:
        packed.append(pair_row)
    return packed + [footer]


def _add_plan_row(ui: dict | None, add_callback: str) -> list[InlineKeyboardButton]:
    return [
        _ikb(
            "➕ افزودن پلن",
            callback_data=add_callback,
            style=_style(ui, "adm_plans_add"),
        )
    ]


def category_icon_for(
    item: object, categories: dict[int, object] | None
) -> str | None:
    """Premium icon id of the category a plan/pack belongs to (None when unset)."""
    cid = getattr(item, "category_id", None)
    if cid is None or not categories:
        return None
    category = categories.get(int(cid))
    return getattr(category, "icon_custom_emoji_id", None) if category else None


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
    # Migrate legacy «دعوت دوستان» key → باشگاه مشتریان
    migrated: list[str] = []
    for p in parts:
        if p == "referral":
            if "loyalty" not in migrated:
                migrated.append("loyalty")
            continue
        migrated.append(p)
    parts = migrated
    known = set(DEFAULT_MENU_ORDER)
    ordered = [p for p in parts if p in known and p not in REMOVED_MENU_KEYS]
    # shop always present
    if "shop" not in ordered:
        ordered.insert(0, "shop")
    return ordered


MENU_SHOW_KEYS = (
    "wallet",
    "support",
    "loyalty",
    "reseller_apply",
    "services",
)


def sync_show_flags_for_order(order: list[str]) -> dict[str, str]:
    """Map menu_order → show_* flags (for persistence / legacy readers)."""
    active = set(order)
    # Legacy menu_order may still list «referral» before migration
    if "referral" in active:
        active.add("loyalty")
    flags = {f"show_{key}": ("1" if key in active else "0") for key in MENU_SHOW_KEYS}
    # Legacy readers still look at show_referral
    flags["show_referral"] = flags.get("show_loyalty", "0")
    return flags


def default_menu_layout_settings(*, for_shop: bool) -> dict[str, str]:
    """Menu order, layout and show flags of a fresh install; a shop never offers «درخواست نمایندگی»."""
    order = [key for key in DEFAULT_ACTIVE_MENU_ORDER if not (for_shop and key == "reseller_apply")]
    payload = {
        "menu_order": ",".join(order),
        "menu_layout": str(DEFAULT_SETTINGS.get("menu_layout") or "compact"),
        **sync_show_flags_for_order(order),
        "show_miniapp": "0",
    }
    if for_shop:
        payload["show_reseller_apply"] = "0"
    return payload


def _connection_status_buttons(key: str, ui: dict | None) -> list[InlineKeyboardButton]:
    if key != CONNECTION_STATUS_MENU_KEY:
        return []
    return [_ikb(_t(ui, "btn_connection_status"), callback_data="csp:refresh", ui=ui, label_key="btn_connection_status")]


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

    buttons: list[InlineKeyboardButton] = []

    for key in _menu_order(ui):
        buttons.extend(_connection_status_buttons(key, ui))
        if key == "shop":
            buttons.append(
                _ikb(
                    _t(ui, "btn_shop"),
                    callback_data="shop:list",
                    style_id="shop", style_fallback="primary",
                    ui=ui,
                    label_key="btn_shop",
                )
            )
        elif key == "services" and has_services:
            buttons.append(
                _ikb(
                    _t(ui, "btn_services"),
                    callback_data="svc:list",
                    style_id="services", style_fallback="primary",
                    ui=ui,
                    label_key="btn_services",
                )
            )
        elif key == "wallet":
            buttons.append(
                _ikb(
                    _t(ui, "btn_wallet"),
                    callback_data="wallet:home",
                    style_id="wallet", style_fallback="primary",
                    ui=ui,
                    label_key="btn_wallet",
                )
            )
        elif key == "support":
            contacts = active_support_contacts(
                parse_support_contacts((ui or {}).get("support_contacts"))
            )
            if len(contacts) == 1:
                url = support_chat_url(contacts[0].get("telegram") or "")
                if url:
                    buttons.append(
                        _ikb(
                            _t(ui, "btn_support"),
                            url=url,
                            ui=ui,
                            label_key="btn_support",
                        )
                    )
                else:
                    buttons.append(
                        _ikb(
                            _t(ui, "btn_support"),
                            callback_data="support:home",
                            ui=ui,
                            label_key="btn_support",
                        )
                    )
            else:
                buttons.append(
                    _ikb(
                        _t(ui, "btn_support"),
                        callback_data="support:home",
                        ui=ui,
                        label_key="btn_support",
                    )
                )
        elif key == "guide":
            buttons.append(
                _ikb(
                    _t(ui, "btn_guide"),
                    callback_data="help:guide",
                    ui=ui,
                    label_key="btn_guide",
                )
            )
        elif key == "faq":
            buttons.append(
                _ikb(
                    _t(ui, "btn_faq"),
                    callback_data="help:faq",
                    ui=ui,
                    label_key="btn_faq",
                )
            )
        elif key in {"loyalty", "referral"}:
            if not on(_t(ui, "loyalty_enabled")):
                continue
            lk = "btn_loyalty" if key == "loyalty" else "btn_referral"
            buttons.append(
                _ikb(
                    _t(ui, lk),
                    callback_data="loy:home",
                    ui=ui,
                    label_key=lk,
                )
            )
        elif key == "reseller_apply" and role == Role.USER.value and not show_reseller_creds:
            buttons.append(
                _ikb(
                    _t(ui, "btn_reseller_apply"),
                    callback_data="resapply:home",
                    ui=ui,
                    label_key="btn_reseller_apply",
                )
            )
        # miniapp: MenuButton Open only — never cataloged here

    # compact = pair left-to-right like the web-panel live preview; classic = one per row
    full_width: list[InlineKeyboardButton] = []
    if role == Role.RESELLER.value:
        # Full shop panel — only on the dedicated reseller bot
        full_width.append(
            _ikb(
                _t(ui, "btn_reseller"),
                callback_data="res:home",
                ui=ui,
                label_key="btn_reseller",
            )
        )
    elif show_reseller_creds:
        # Main bot: credentials / deep-link (capacity renew is on reply KB)
        full_width.append(
            _ikb(
                _t(ui, "btn_reseller_creds"),
                callback_data="res:creds",
                ui=ui,
                label_key="btn_reseller_creds",
            )
        )
    if role == Role.ADMIN.value and as_user:
        full_width.append(
            _ikb(
                _t(ui, "btn_admin"),
                callback_data="nv:adm:home",
                ui=ui,
                label_key="btn_admin",
            )
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
REPLY_ACTION_REFERRAL = "referral"  # legacy alias → loyalty referral subset
REPLY_ACTION_LOYALTY = "loyalty"
REPLY_ACTION_LOY_REFERRAL = "loy_referral"
REPLY_ACTION_LOY_POINTS = "loy_points"
REPLY_ACTION_LOY_REWARDS = "loy_rewards"
REPLY_ACTION_LOY_WHEEL = "loy_wheel"
REPLY_ACTION_LOY_HISTORY = "loy_history"
REPLY_ACTION_ADMIN_LOYALTY = "adm_loyalty"
REPLY_ACTION_ADM_LOY_OVERVIEW = "adm_loy_overview"
REPLY_ACTION_ADM_LOY_RULES = "adm_loy_rules"
REPLY_ACTION_ADM_LOY_REWARDS = "adm_loy_rewards"
REPLY_ACTION_ADM_LOY_TIERS = "adm_loy_tiers"
REPLY_ACTION_ADM_LOY_SETTINGS = "adm_loy_settings"
REPLY_ACTION_ADM_LOY_REF_TEXT = "adm_loy_ref_text"
REPLY_ACTION_RESELLER_APPLY = "reseller_apply"
REPLY_ACTION_MINIAPP = "miniapp"
REPLY_ACTION_RESELLER = "reseller"
REPLY_ACTION_CREDS = "reseller_creds"
REPLY_ACTION_ADMIN = "admin"
REPLY_ACTION_ADM_HUB_OPS = "adm_hub_ops"
REPLY_ACTION_ADM_HUB_PEOPLE = "adm_hub_people"
REPLY_ACTION_ADM_HUB_PRODUCT = "adm_hub_product"
REPLY_ACTION_ADM_HUB_MARKETING = "adm_hub_marketing"
REPLY_ACTION_ADM_HUB_SYSTEM = "adm_hub_system"
REPLY_ACTION_ADMIN_DASH = "adm_dash"
REPLY_ACTION_ADMIN_ORDERS = "adm_orders"
REPLY_ACTION_ADMIN_REPORTS = "adm_reports"
REPLY_ACTION_ADMIN_PAYMENTS = "adm_payments"
REPLY_ACTION_ADMIN_TICKETS = "adm_tickets"
REPLY_ACTION_ADMIN_PLANS = "adm_plans"
REPLY_ACTION_ADMIN_PG = "adm_pg"
REPLY_ACTION_ADMIN_PREVIEW = "adm_preview"
REPLY_ACTION_RES_PREVIEW = "res_preview"
REPLY_ACTION_RES_ADD_REP = "res_add_rep"
REPLY_ACTION_ADMIN_USERS = "adm_users"
REPLY_ACTION_ADMIN_SETTINGS = "adm_settings"
REPLY_ACTION_ADM_ST_PANEL = "adm_st_panel"
REPLY_ACTION_RES_ST_PANEL = "res_st_panel"
REPLY_ACTION_ADMIN_BROADCAST = "adm_broadcast"
REPLY_ACTION_ADMIN_RESELLERS = "adm_resellers"
REPLY_ACTION_ADMIN_BACKUP = "adm_backup"
REPLY_ACTION_PAY_WALLET = "pay_wallet"
REPLY_ACTION_PAY_CARD = "pay_card"
REPLY_ACTION_PAY_GATEWAY = "pay_gateway"
REPLY_ACTION_PAY_CRYPTO = "pay_crypto"
REPLY_ACTION_PAY_STARS = "pay_stars"
REPLY_ACTION_PAY_PSP = "pay_psp"
REPLY_ACTION_PAY_BBC = "pay_bbc"
REPLY_ACTION_PAY_DISCOUNT = "pay_discount"
REPLY_ACTION_TOPUP_CARD = "topup_card"
REPLY_ACTION_TOPUP_GATEWAY = "topup_gateway"
REPLY_ACTION_TOPUP_PSP = "topup_psp"
REPLY_ACTION_TOPUP_BBC = "topup_bbc"
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


# Bot API 9.4 button colors — defaults live in app.services.button_styles;
# panel tab «رنگبندی دکمه‌ها» overrides via btn_style_* settings.
_STYLE_AUTO = object()


def _reply_style_id(action: str | None, text: str | None) -> tuple[str | None, str | None]:
    """Catalog style id (and fallback) a reply button takes its color from."""
    from app.services.button_styles import CATALOG_IDS

    a = (action or "").strip()
    if a in CATALOG_IDS:
        return a, None
    t = (text or "").strip()
    if t in {"انصراف", "لغو", "cancel"} or (
        t and "انصراف" in t and "بازگشت" not in t
    ):
        return "cancel", "danger"
    return None, None


def _reply_btn_style(
    action: str | None,
    text: str | None = None,
    ui: dict | None = None,
) -> str | None:
    """Map reply-keyboard actions to Bot API button styles (settings-aware)."""
    from app.services.button_styles import style_or_none

    style_id, fallback = _reply_style_id(action, text)
    if style_id is None:
        return None
    return style_or_none(ui, style_id, fallback=fallback)


def _label_source_key(
    ui: dict | None, text: str, *, label_key: str | None, action: str | None
) -> str | None:
    """btn_* setting the button text comes from; None when the text is fixed in code.

    An action alone only counts when the shown text really starts with that
    setting's label (a queue badge may follow it), so a button that shares an
    action but carries its own wording is never offered as editable.
    """
    if label_key:
        return label_key
    from app.services.rich_text import btn_setting_key_for_action

    key = btn_setting_key_for_action(action)
    if not key or key not in DEFAULT_SETTINGS:
        return None
    label = (_t(ui, key) or "").strip()
    return key if label and (text or "").strip().startswith(label) else None


def _kb(
    text: str,
    *,
    action: str | None = None,
    style: object = _STYLE_AUTO,
    ui: dict | None = None,
    label_key: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> KeyboardButton:
    style_id: str | None = None
    if style is _STYLE_AUTO:
        st = _reply_btn_style(action, text, ui=ui)
        style_id = _reply_style_id(action, text)[0]
    else:
        st = style if style else None
    icon = icon_custom_emoji_id
    if icon is None and ui is not None:
        from app.services.rich_text import button_icon_from_ui

        icon = button_icon_from_ui(ui, label_key=label_key, action=action)
    kwargs: dict = {"text": text}
    if st:
        kwargs["style"] = str(st)
    if icon:
        kwargs["icon_custom_emoji_id"] = str(icon)
    button = KeyboardButton(**kwargs)
    if is_recording():
        note_button_source(
            button,
            label_key=_label_source_key(ui, text, label_key=label_key, action=action),
            style_id=style_id,
        )
    return button


def _ikb(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
    ui: dict | None = None,
    label_key: str | None = None,
    action: str | None = None,
    icon_custom_emoji_id: str | None = None,
    style_id: str | None = None,
    style_fallback: str | None = None,
    **extra,
) -> InlineKeyboardButton:
    """Inline button; pass ``style_id`` (not a resolved ``style``) so the color stays editable."""
    if style_id is not None:
        style = _style(ui, style_id, fallback=style_fallback)
    button = _build_inline_button(
        text,
        callback_data=callback_data,
        url=url,
        style=style,
        ui=ui,
        label_key=label_key,
        action=action,
        icon_custom_emoji_id=icon_custom_emoji_id,
        **extra,
    )
    if is_recording():
        note_button_source(
            button,
            label_key=_label_source_key(ui, text, label_key=label_key, action=action),
            style_id=style_id,
        )
    return button


def _build_inline_button(
    text: str,
    *,
    callback_data: str | None,
    url: str | None,
    style: str | None,
    ui: dict | None,
    label_key: str | None,
    action: str | None,
    icon_custom_emoji_id: str | None,
    **extra,
) -> InlineKeyboardButton:
    kwargs: dict = {"text": text, **extra}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    # web_app + icon_custom_emoji_id → some Telegram clients open the sheet
    # with empty initData (reply KB and inline). Never combine them.
    if kwargs.get("web_app") is not None:
        kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(**kwargs)
    icon = icon_custom_emoji_id
    if icon is None and ui is not None:
        from app.services.rich_text import button_icon_from_ui

        icon = button_icon_from_ui(ui, label_key=label_key, action=action)
    if icon and "icon_custom_emoji_id" not in kwargs:
        kwargs["icon_custom_emoji_id"] = str(icon)
    return InlineKeyboardButton(**kwargs)


def _style(ui: dict | None, button_id: str, *, fallback: str | None = None) -> str | None:
    from app.services.button_styles import style_or_none

    return style_or_none(ui, button_id, fallback=fallback)


def back_button(ui: dict | None, callback_data: str) -> InlineKeyboardButton:
    """Back button with the configured label, premium icon and style."""
    return _ikb(
        _label_or(ui, "btn_back", "⬅️ بازگشت"),
        callback_data=callback_data,
        style_id="back",
        ui=ui,
        label_key="btn_back",
    )


def _plan_row_style(
    ui: dict | None,
    plan: object,
    *,
    kind: str | None = None,
    audience: str = "users",
    category: object | None = None,
) -> str | None:
    from app.services.button_styles import resolve_plan_button_style

    return resolve_plan_button_style(
        ui, plan, kind=kind, audience=audience, category=category
    )


def _category_button_label(cat: object) -> str:
    """Plain category name — never force a 📁 prefix (premium icon is separate)."""
    name = (getattr(cat, "name", None) or "").strip()
    return name or "دسته"


def plan_button_style_picker_keyboard(
    *,
    callback_prefix: str,
    back_callback: str,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Per-plan color picker — option buttons show their own Telegram style."""
    from app.services.button_styles import PLAN_BUTTON_STYLE_OPTIONS

    rows: list[list[InlineKeyboardButton]] = []
    for val, label, _tone in PLAN_BUTTON_STYLE_OPTIONS:
        cb_val = "inherit" if val == "inherit" else (val or "default")
        opt_style = None if val in {"inherit", ""} else val
        rows.append(
            [
                _ikb(
                    label,
                    callback_data=f"{callback_prefix}:{cb_val}",
                    style=opt_style,
                )
            ]
        )
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data=back_callback,
                style_id="back",
                ui=ui,
                label_key="btn_back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_button_style_picker_keyboard(
    *,
    callback_prefix: str,
    back_callback: str,
    inherit_label: str = "ارث از پیش‌فرض",
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Per-item color picker for JSON list entries (support, payment destinations)."""
    from app.services.button_styles import item_button_style_options

    rows: list[list[InlineKeyboardButton]] = []
    for val, label, _tone in item_button_style_options(inherit_label=inherit_label):
        cb_val = "inherit" if val == "inherit" else (val or "default")
        opt_style = None if val in {"inherit", ""} else val
        rows.append(
            [
                _ikb(
                    label,
                    callback_data=f"{callback_prefix}:{cb_val}",
                    style=opt_style,
                )
            ]
        )
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data=back_callback,
                style_id="back",
                ui=ui,
                label_key="btn_back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Reply keyboards live in app.bot.reply_keyboards (re-exported for compatibility).
from app.bot.reply_keyboards import (  # noqa: E402
    _reply_markup,
    _reply_user_entries,
    _reply_admin_hub_entries,
    _reply_admin_ops_entries,
    _reply_admin_people_entries,
    _reply_admin_product_entries,
    _reply_admin_system_entries,
    _reply_admin_entries,
    _pg_submenu_entries,
    _admin_users_submenu_entries,
    _admin_resellers_submenu_entries,
    _admin_settings_submenu_entries,
    _admin_backup_submenu_entries,
    _admin_broadcast_submenu_entries,
    _admin_plans_submenu_entries,
    _admin_plans_audience_entries,
    _admin_plans_list_entries,
    _admin_plans_add_type_entries,
    _admin_plans_kind_entries,
    REPLY_ACTION_ADM_PLANS_AUD_USERS,
    REPLY_ACTION_ADM_PLANS_AUD_RESELLERS,
    REPLY_ACTION_ADM_PLANS_ADD,
    REPLY_ACTION_ADM_PLANS_CATEGORIES,
    REPLY_ACTION_ADM_PLANS_ADDONS,
    REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
    REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM,
    REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL,
    REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE,
    REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
    REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
    REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
    REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
    REPLY_ACTION_RES_PLAN_CATEGORIES,
    REPLY_ACTION_RES_PLAN_ADDONS,
    _reseller_plans_submenu_entries,
    _reseller_settings_submenu_entries,
    _wallet_submenu_entries,
    _support_submenu_entries,
    _loyalty_submenu_entries,
    _admin_loyalty_submenu_entries,
    _reseller_capacity_entries,
    _reseller_submenu_entries,
    reseller_hub_main_keyboard,
    _pay_method_entries,
    _topup_method_entries,
    _pack_reply_rows,
    _submenu_footer,
    main_reply_keyboard,
    admin_reply_keyboard,
    admin_ops_reply_keyboard,
    admin_people_reply_keyboard,
    admin_product_reply_keyboard,
    admin_system_reply_keyboard,
    wallet_reply_keyboard,
    support_reply_keyboard,
    loyalty_reply_keyboard,
    admin_loyalty_reply_keyboard,
    reseller_reply_keyboard,
    pay_reply_keyboard,
    topup_pay_reply_keyboard,
    pg_reply_keyboard,
    admin_users_reply_keyboard,
    admin_resellers_reply_keyboard,
    admin_settings_reply_keyboard,
    admin_backup_reply_keyboard,
    admin_broadcast_reply_keyboard,
    admin_plans_audience_reply_keyboard,
    admin_plans_kind_reply_keyboard,
    admin_plans_add_type_reply_keyboard,
    admin_plans_reply_keyboard,
    reseller_settings_reply_keyboard,
    reseller_plans_reply_keyboard,
    reply_action_map,
    _shop_submenu_entries,
    shop_reply_keyboard,
    reseller_apply_reply_keyboard,
    submenu_chrome_reply_keyboard,
    _service_action_entries,
    REPLY_ACTION_SVC_CANCEL as REPLY_ACTION_SVC_CANCEL,
    service_actions_reply_keyboard,
    _review_submenu_entries,
    review_reply_keyboard,
    cancel_reply,
    persistent_reply_keyboard,
)

RESELLER_PLANS_PAGE_SIZE = 10


def reseller_plans_list_keyboard(
    plans: list, ui: dict | None = None, *, page_index: int = 0
) -> InlineKeyboardMarkup:
    """One page of shop plan rows; they inherit the fixed-kind color (per-plan override supported)."""
    page = page_of(plans, page_index, RESELLER_PLANS_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = []
    for p in page.items:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        price = getattr(p, "price", 0) or 0
        label = f"{flag} {name} · {price:,}"
        rows.append(
            [
                _ikb(
                    label,
                    callback_data=f"res:plan:view:{p.id}",
                    style=_plan_row_style(ui, p, kind="fixed"),
                )
            ]
        )
    kind_style = _style(ui, "shop_kind_fixed", fallback="primary")
    if not rows:
        rows.append(
            [
                _ikb(
                    "پلنی نیست — از کیبورد «پلن جدید»",
                    callback_data="res:plans",
                    style=kind_style,
                )
            ]
        )
    if pager := pager_row(page, "res:plans"):
        rows.append(pager)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def force_join_inline_keyboard(
    raw_channels: str | None,
    *,
    ui: dict | None = None,
    channels: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """One URL inline button per required channel + «عضو شدم» re-check callback.

    Channel targets live on these buttons (not as a text list). Optional per-channel
    ``title`` becomes the button label; otherwise ``btn_force_join`` (+ @username).
    """
    from app.services.users import (
        force_join_channel_url,
        parse_force_join_channels,
        parse_force_join_entries,
    )

    entries = parse_force_join_entries(raw_channels)
    by_id = {
        str(e.get("id") or "").lower(): e
        for e in entries
        if e.get("id")
    }
    required = channels if channels is not None else parse_force_join_channels(raw_channels)
    btn_join = (_t(ui, "btn_force_join") or "عضویت در کانال").strip()
    rows: list[list[InlineKeyboardButton]] = []
    for ch in required:
        entry = by_id.get(str(ch).lower()) or {}
        url = force_join_channel_url(ch, entry.get("link"))
        if not url:
            # Still show a non-url hint row so admins notice missing invite link
            label = (str(entry.get("title") or "").strip() or btn_join)[:64]
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"⚠️ {label}"[:64],
                        callback_data="forcejoin:nolink",
                    )
                ]
            )
            continue
        title = str(entry.get("title") or "").strip()
        if title:
            label = title
        elif str(ch).startswith("@"):
            label = f"{btn_join} · {ch}"
        else:
            label = btn_join
        rows.append([InlineKeyboardButton(text=label[:64], url=url)])
    rows.append(
        [
            _ikb(
                _t(ui, "btn_force_join_check"),
                callback_data="forcejoin:check",
                style_id="force_join_check", style_fallback="primary",
                ui=ui,
                label_key="btn_force_join_check",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)



def terms_inline_keyboard(prompt, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Single accept button for a terms gate (entry / purchase)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    prompt.btn_label,
                    callback_data=f"terms:ok:{prompt.gate}",
                    style=_style(ui, prompt.style_id, fallback="primary"),
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

# Legacy admin plan reply labels (pre unified kind picker)
REPLY_ACTION_ADM_PLAN_ADD = "adm_plan_add"
REPLY_ACTION_ADM_PLAN_CUSTOM = "adm_plan_custom"
REPLY_ACTION_ADM_PLAN_TRIAL = "adm_plan_trial"


def shop_kind_keyboard(
    ui: dict | None = None,
    *,
    fixed_on: bool = False,
    trial_on: bool = False,
    custom_on: bool = False,
    wholesale_on: bool = False,
    categories: list | None = None,
    include_uncategorized: bool = False,
) -> InlineKeyboardMarkup:
    """Step 1 of user shop — categories replace «ثابت» when configured.

    Legacy shops with no linked categories still get the «ثابت» button.
    Trial / custom / wholesale remain sibling entries when enabled.
    """
    from app.services.button_styles import resolve_category_button_style

    rows: list[list[InlineKeyboardButton]] = []
    # Trial first when eligible — discoverability after welcome CTAs were removed.
    # Labels come from settings when present; never re-prepend a hardcoded emoji.
    if trial_on:
        rows.append(
            [
                _ikb(
                    _label_or(ui, "btn_shop_kind_trial", "تست"),
                    callback_data="shop:kind:trial",
                    style_id="shop_kind_trial", style_fallback="primary",
                    ui=ui,
                    label_key="btn_shop_kind_trial",
                )
            ]
        )
    cats = list(categories or [])
    if fixed_on and cats:
        for cat in cats:
            rows.append(
                [
                    _ikb(
                        _category_button_label(cat),
                        callback_data=f"shop:cat:{int(cat.id)}",
                        style=resolve_category_button_style(ui, cat),
                        icon_custom_emoji_id=getattr(cat, "icon_custom_emoji_id", None),
                    )
                ]
            )
        if include_uncategorized:
            rows.append(
                [
                    _ikb(
                        "سایر",
                        callback_data="shop:cat:none",
                        style=_style(ui, "shop_kind_fixed", fallback="primary"),
                    )
                ]
            )
    elif fixed_on:
        rows.append(
            [
                _ikb(
                    _label_or(ui, "btn_shop_kind_fixed", "ثابت"),
                    callback_data="shop:kind:fixed",
                    style_id="shop_kind_fixed", style_fallback="primary",
                    ui=ui,
                    label_key="btn_shop_kind_fixed",
                )
            ]
        )
    if custom_on:
        from app.services.button_styles import resolve_custom_plan_button_style

        rows.append(
            [
                _ikb(
                    _label_or(ui, "btn_shop_kind_custom", "دلخواه"),
                    callback_data="shop:kind:custom",
                    style=resolve_custom_plan_button_style(ui),
                    ui=ui,
                    label_key="btn_shop_kind_custom",
                )
            ]
        )
    if wholesale_on:
        label = _t(ui, "btn_wholesale") or "📦 فروش عمده"
        rows.append(
            [
                _ikb(
                    label,
                    callback_data="shop:kind:wholesale",
                    style_id="shop_kind_wholesale", style_fallback="primary",
                    ui=ui,
                    label_key="btn_wholesale",
                )
            ]
        )
    if not rows:
        rows = [[InlineKeyboardButton(text=feedback.NO_PLANS, callback_data="menu:home")]]
    else:
        rows = chunk_buttons([row[0] for row in rows], cols=2)
        # Same as wallet/support/loyalty hubs — back to welcome (menu:home).
        # Nested plan lists keep back_callback="shop:list" (kind picker), not home.
        rows.append(
            [
                _ikb(
                    _t(ui, "btn_back") or "⬅️ بازگشت",
                    callback_data="menu:home",
                    ui=ui,
                    label_key="btn_back",
                    style_id="back",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_plan_kind_keyboard(
    audience: str,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Admin plans — kind step for the chosen audience."""
    aud = (audience or "users").strip()
    back = back_button(ui, "adm:plans")
    if aud == "resellers":
        rows = [
            [
                _ikb(
                    "اشتراک ثابت",
                    callback_data="adm:plans:kind:resellers:fixed",
                    style=_style(ui, "plan_res_fixed", fallback="primary"),
                )
            ],
            [
                _ikb(
                    "اشتراک PAYG",
                    callback_data="adm:plans:kind:resellers:payg",
                    style=_style(ui, "plan_res_payg", fallback="primary"),
                )
            ],
            [
                _ikb(
                    "بسته حجم",
                    callback_data="adm:plans:kind:resellers:addon_volume",
                    style=_style(ui, "plan_res_addon_vol", fallback="primary"),
                )
            ],
            [
                _ikb(
                    "بسته کاربر",
                    callback_data="adm:plans:kind:resellers:addon_users",
                    style=_style(ui, "plan_res_addon_users", fallback="primary"),
                )
            ],
            [back],
        ]
        return InlineKeyboardMarkup(inline_keyboard=pack_detail_rows(rows))
    rows = [
        [
            _ikb(
                _label_or(ui, "btn_shop_kind_fixed", "ثابت"),
                callback_data="adm:plans:kind:users:fixed",
                style_id="shop_kind_fixed", style_fallback="primary",
                ui=ui,
                label_key="btn_shop_kind_fixed",
            )
        ],
        [
            _ikb(
                _label_or(ui, "btn_shop_kind_custom", "دلخواه"),
                callback_data="adm:plans:kind:users:custom",
                style_id="shop_kind_custom", style_fallback="primary",
                ui=ui,
                label_key="btn_shop_kind_custom",
            )
        ],
        [
            _ikb(
                _label_or(ui, "btn_shop_kind_trial", "تست"),
                callback_data="adm:plans:kind:users:trial",
                style_id="shop_kind_trial", style_fallback="primary",
                ui=ui,
                label_key="btn_shop_kind_trial",
            )
        ],
        [
            _ikb(
                _label_or(ui, "btn_wholesale", "فروش عمده"),
                callback_data="adm:plans:kind:users:wholesale",
                style_id="shop_kind_wholesale", style_fallback="primary",
                ui=ui,
                label_key="btn_wholesale",
            )
        ],
        [back],
    ]
    return InlineKeyboardMarkup(inline_keyboard=pack_detail_rows(rows))


def plans_keyboard(
    plans: list[Plan],
    ui: dict | None = None,
    *,
    custom_enabled: bool = False,
    wholesale_enabled: bool = False,
    back_callback: str = "shop:list",
    kind: str | None = None,
    category_names: dict[int, str] | None = None,
    categories: dict[int, object] | None = None,
) -> InlineKeyboardMarkup:
    """Plan name rows — override → category inherit → plan-kind catalog color."""
    from app.services.button_styles import plan_kind_style_id

    _ = custom_enabled, wholesale_enabled  # call-site compat
    fallback_kind = kind or "fixed"
    kind_style = _style(
        ui, plan_kind_style_id(fallback_kind) or "shop_kind_fixed", fallback="primary"
    )
    cat_names = category_names or {}
    cat_objs = categories or {}
    rows = []
    for p in plans:
        cat = ""
        cid = getattr(p, "category_id", None)
        cat_obj = None
        if cid is not None:
            cid_i = int(cid)
            if cid_i in cat_names:
                cat = f"[{cat_names[cid_i]}] "
            cat_obj = cat_objs.get(cid_i)
        rows.append(
            [
                _ikb(
                    f"{cat}{p.name} — {sale_price(p):,} ت".replace(",", "٬"),
                    callback_data=f"shop:plan:{p.id}",
                    style=_plan_row_style(
                        ui, p, kind=kind or fallback_kind, category=cat_obj
                    ),
                    icon_custom_emoji_id=category_icon_for(p, cat_objs),
                )
            ]
        )
    rows = _pack_by_label_width([row[0] for row in rows])
    if not rows:
        rows = [
            [
                _ikb(
                    feedback.NO_PLANS,
                    callback_data=back_callback,
                    style=kind_style,
                )
            ]
        ]
    if back_callback:
        rows.append([back_button(ui, back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wholesale_plans_keyboard(
    plans: list[Plan],
    ui: dict | None = None,
    *,
    back_callback: str = "shop:list",
    categories: dict[int, object] | None = None,
) -> InlineKeyboardMarkup:
    """Wholesale plan names — Back returns to shop kind hub (not this screen)."""
    rows = _pack_by_label_width(
        [
            _ikb(
                f"{p.name} — {sale_price(p):,} ت".replace(",", "٬"),
                callback_data=f"shop:wholesale:plan:{p.id}",
                style=_plan_row_style(ui, p, kind="wholesale"),
                icon_custom_emoji_id=category_icon_for(p, categories),
            )
            for p in plans
        ]
    )
    kind_style = _style(ui, "shop_kind_wholesale", fallback="primary")
    if not rows:
        rows = [
            [
                _ikb(
                    feedback.NO_PLANS,
                    callback_data=back_callback,
                    style=kind_style,
                )
            ]
        ]
    if back_callback:
        rows.append(
            [
                _ikb(
                    _t(ui, "btn_back"),
                    callback_data=back_callback,
                    style_id="back",
                    ui=ui,
                    label_key="btn_back",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wholesale_qty_keyboard(
    qty: int,
    ui: dict | None = None,
    *,
    plan_id: int,
) -> InlineKeyboardMarkup:
    kind_style = _style(ui, "shop_kind_wholesale", fallback="primary")
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ikb("➖", callback_data="shop:wholesale:qty:-", style=kind_style),
            _ikb(f"{qty} عدد", callback_data="shop:wholesale:noop", style=kind_style),
            _ikb("➕", callback_data="shop:wholesale:qty:+", style=kind_style),
        ],
        [
            _ikb(
                "✏️ ورود دستی تعداد",
                callback_data="shop:wholesale:qty:input",
                style=kind_style,
            )
        ],
        [
            _ikb(
                _t(ui, "btn_buy_continue") or "🟢✅ ادامه خرید",
                callback_data="shop:wholesale:confirm",
                style=_style(ui, "buy_continue", fallback="primary") or kind_style,
            )
        ],
        [
            _ikb(
                _t(ui, "btn_back"),
                callback_data="shop:kind:wholesale",
                style_id="back",
                ui=ui,
                label_key="btn_back",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        [back_button(ui, "shop:list")],
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
            [
                _ikb(
                    _t(ui, "btn_buy_continue") or "🟢✅ ادامه خرید",
                    callback_data="shop:custom:confirm",
                    style_id="buy_continue", style_fallback="primary",
                    ui=ui,
                    label_key="btn_buy_continue",
                )
            ],
            [back_button(ui, "shop:custom")],
        ]
    )


def plan_actions(plan_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Buy confirm under the plan message — Back returns to plan list."""
    buy_label = _t(ui, "btn_buy_continue") or "🟢✅ ادامه خرید"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    buy_label,
                    callback_data=f"shop:buy:{plan_id}",
                    style_id="buy_continue", style_fallback="primary",
                    ui=ui,
                    label_key="btn_buy_continue",
                )
            ],
            [back_button(ui, "shop:list")],
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
            "pay_psp_enabled",
            "pay_bbc_enabled",
            "pay_crypto_enabled",
            "pay_stars_enabled",
        )
    )


_PAY_METHOD_STYLE_FALLBACKS: tuple[tuple[str, str | None], ...] = (
    ("card", "primary"),
    ("gateway", "primary"),
    ("psp", "primary"),
    ("bbc", "primary"),
    ("crypto", "primary"),
    ("stars", "primary"),
    ("discount", None),
)


def _pay_method_button(
    ui: dict | None,
    order_id: int,
    method: str,
    style_fallback: str | None,
) -> InlineKeyboardButton:
    return _ikb(
        _t(ui, f"btn_pay_{method}"),
        callback_data=f"pay:{method}:{order_id}",
        style_id=f"pay_{method}", style_fallback=style_fallback,
        ui=ui,
        label_key=f"btn_pay_{method}",
    )


def pay_methods(order_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if on(_t(ui, "pay_wallet_enabled")):
        rows.append([_pay_method_button(ui, order_id, "wallet", "success")])
    others = [
        _pay_method_button(ui, order_id, method, fallback)
        for method, fallback in _PAY_METHOD_STYLE_FALLBACKS
        if on(_t(ui, f"pay_{method}_enabled"))
    ]
    rows.extend(chunk_buttons(others, cols=2))
    rows.append(
        [
            back_button(ui, f"pay:back:{order_id}"),
            _ikb(
                _t(ui, "btn_cancel"),
                callback_data="menu:home",
                style_id="cancel", style_fallback="danger",
                ui=ui,
                label_key="btn_cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_keyboard(
    services: list,
    ui: dict | None = None,
    *,
    reseller_capacity_label: str | None = None,
) -> InlineKeyboardMarkup:
    """Entity names only — actions/back live on reply keyboard."""
    _ = ui
    buttons = [
        InlineKeyboardButton(
            text=f"🔹 {s.pg_username}",
            callback_data=f"svc:view:{s.id}",
        )
        for s in services[:LIST_BUTTON_CAP]
    ]
    rows = chunk_buttons(buttons, cols=2)
    if reseller_capacity_label:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text=str(reseller_capacity_label)[:64],
                    callback_data="svc:reseller",
                )
            ],
        )
    if not rows:
        rows = [[InlineKeyboardButton(text="سرویسی نیست", callback_data="svc:list")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


REPLY_ACTION_SVC_LINK = "svc_link"
REPLY_ACTION_SVC_GUIDE = "svc_guide"
REPLY_ACTION_SVC_RENEW = "svc_renew"
REPLY_ACTION_SVC_ADDON = "svc_addon"
REPLY_ACTION_SVC_AUTO = "svc_auto"
REPLY_ACTION_SVC_REFRESH = "svc_refresh"
REPLY_ACTION_SVC_DELETE = "svc_delete"


def service_actions(service_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — prefer service_actions_reply_keyboard."""
    _ = service_id, ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def support_contacts_keyboard(contacts: list[dict], ui: dict | None = None) -> InlineKeyboardMarkup:
    """URL contact rows only — ticket/back live on support_reply_keyboard."""
    from app.services.button_styles import resolve_support_contact_style

    buttons: list[InlineKeyboardButton] = []
    for c in contacts:
        url = support_chat_url(c.get("telegram") or "")
        title = str(c.get("title") or "پشتیبان")[:64]
        if url:
            style = resolve_support_contact_style(ui, c)
            kwargs: dict = {"text": f"💬 {title}", "url": url}
            if style:
                kwargs["style"] = style
            buttons.append(InlineKeyboardButton(**kwargs))
    return InlineKeyboardMarkup(
        inline_keyboard=chunk_buttons(buttons[:LIST_BUTTON_CAP], cols=2)
    )


def admin_home(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy inline admin hub — prefer admin_reply_keyboard (3.6+).

    Kept for rare edit_text fallbacks; callers should migrate to reply KB.
    """
    back = back_button(ui, "menu:home")
    return InlineKeyboardMarkup(inline_keyboard=[[back]])


def admin_users_plans_overview_keyboard(
    plans: list,
    ui: dict | None = None,
    *,
    back_callback: str | None = "adm:plans",
) -> InlineKeyboardMarkup:
    """User plan rows inline — mirrors web /plans user table; no back row when ``back_callback`` is None."""
    from app.services.button_styles import resolve_custom_plan_button_style

    wholesale_label = _label_or(ui, "btn_wholesale", "فروش عمده")
    trial_on = on((ui or {}).get("trial_enabled"))
    custom_on = on((ui or {}).get("custom_plan_enabled"))
    wholesale_on = on((ui or {}).get("wholesale_enabled"))
    fixed_style = _style(ui, "shop_kind_fixed", fallback="primary")
    trial_style = _style(ui, "shop_kind_trial", fallback="primary")
    custom_style = resolve_custom_plan_button_style(ui)
    wholesale_style = _style(ui, "shop_kind_wholesale", fallback="primary")
    trial_label = _label_or(ui, "btn_shop_kind_trial", "تست")
    custom_label = _label_or(ui, "btn_shop_kind_custom", "دلخواه")
    rows: list[list[InlineKeyboardButton]] = []
    fixed = [p for p in plans if not getattr(p, "is_trial", False)]
    for p in fixed[:LIST_BUTTON_CAP]:
        warn = ""
        if not getattr(p, "pg_template_id", None) and not (getattr(p, "pg_group_ids", None) or "").strip():
            warn = " ⚠️"
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        rows.append(
            [
                _ikb(
                    f"{flag} {name}{warn}"[:60],
                    callback_data=f"adm:plan:view:{p.id}",
                    style=_plan_row_style(ui, p, kind="fixed"),
                )
            ]
        )
    if not fixed:
        rows.append(
            [
                _ikb(
                    "پلن ثابتی نیست",
                    callback_data="adm:plans:noop",
                    style=fixed_style,
                )
            ]
        )
    trial = next((p for p in plans if getattr(p, "is_trial", False)), None)
    trial_flag = "✅" if trial_on else "⏸"
    trial_hint = f" · {trial.name}" if trial else ""
    trial_row_style = (
        _plan_row_style(ui, trial, kind="trial") if trial else trial_style
    )
    rows.append(
        [
            _ikb(
                f"{trial_flag} {trial_label}{trial_hint}"[:60],
                callback_data="adm:plans:kind:users:trial",
                style=trial_row_style,
                ui=ui,
                label_key="btn_shop_kind_trial",
            )
        ]
    )
    rows.append(
        [
            _ikb(
                f"{'✅' if custom_on else '⏸'} {custom_label}",
                callback_data="adm:plans:kind:users:custom",
                style=custom_style,
                ui=ui,
                label_key="btn_shop_kind_custom",
            )
        ]
    )
    rows.append(
        [
            _ikb(
                f"{'✅' if wholesale_on else '⏸'} {wholesale_label}"[:60],
                callback_data="adm:plans:kind:users:wholesale",
                style=wholesale_style,
                ui=ui,
                label_key="btn_wholesale",
            )
        ]
    )
    rows = rows[:-3] + chunk_buttons([row[0] for row in rows[-3:]], cols=2)
    if back_callback:
        rows.append([back_button(ui, back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_resellers_plans_overview_keyboard(
    fixed_plans: list,
    payg_plans: list,
    ui: dict | None = None,
    addon_plans: list | None = None,
    *,
    back_callback: str | None = "adm:plans",
) -> InlineKeyboardMarkup:
    """Reseller subscription + addon packs — mirrors web /plans sections; no back row when ``back_callback`` is None."""
    fixed_style = _style(ui, "plan_res_fixed", fallback="primary")
    payg_style = _style(ui, "plan_res_payg", fallback="primary")
    addon_style = _style(ui, "plan_res_fixed", fallback="primary")
    addons = addon_plans or []
    rows: list[list[InlineKeyboardButton]] = []
    for p in fixed_plans[: LIST_BUTTON_CAP - len(rows)]:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        rows.append(
            [
                _ikb(
                    f"{flag} 📦 {name}"[:60],
                    callback_data=f"adm:resplan:view:{p.id}",
                    style=fixed_style,
                )
            ]
        )
    for p in payg_plans[: LIST_BUTTON_CAP - len(rows)]:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        rate = int(getattr(p, "price_per_gb", 0) or 0)
        extra = f" · {rate:,} ت/گیگ".replace(",", "٬") if rate else ""
        rows.append(
            [
                _ikb(
                    f"{flag} ⚡ {name}{extra}"[:60],
                    callback_data=f"adm:resplan:view:{p.id}",
                    style=payg_style,
                )
            ]
        )
    for p in addons[: LIST_BUTTON_CAP - len(rows)]:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        kind = str(getattr(p, "plan_kind", "") or "")
        if kind == "addon_users":
            qty = int(getattr(p, "addon_users", 0) or 0)
            tag = f"+{qty} کاربر"
        else:
            qty = int(getattr(p, "addon_gb", 0) or 0)
            tag = f"+{qty} گیگ"
        rows.append(
            [
                _ikb(
                    f"{flag} {name} · {tag}"[:60],
                    callback_data=f"adm:resplan:view:{p.id}",
                    style=addon_style,
                )
            ]
        )
    if not fixed_plans and not payg_plans and not addons:
        rows.append(
            [
                _ikb(
                    feedback.NO_PLANS,
                    callback_data="adm:plans:noop",
                    style=fixed_style,
                )
            ]
        )
    if back_callback:
        rows.append([back_button(ui, back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_plans_add_type_keyboard(audience: str, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Inline type picker after «افزودن پلن» — mirrors web modal audience→kind."""
    wholesale_label = _label_or(ui, "btn_wholesale", "فروش عمده")
    back_aud = "users" if audience == "users" else "resellers"
    rows: list[list[InlineKeyboardButton]] = []
    if audience == "resellers":
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="اشتراک ثابت",
                        callback_data="adm:plans:add:resellers:fixed",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="اشتراک PAYG",
                        callback_data="adm:plans:add:resellers:payg",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="بسته حجم",
                        callback_data="adm:plans:add:resellers:addon_volume",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="بسته کاربر",
                        callback_data="adm:plans:add:resellers:addon_users",
                    )
                ],
            ]
        )
    else:
        rows.extend(
            [
                [
                    _ikb(
                        _label_or(ui, "btn_shop_kind_fixed", "ثابت"),
                        callback_data="adm:plans:add:users:fixed",
                        ui=ui,
                        label_key="btn_shop_kind_fixed",
                    )
                ],
                [
                    _ikb(
                        _label_or(ui, "btn_shop_kind_custom", "دلخواه"),
                        callback_data="adm:plans:add:users:custom",
                        ui=ui,
                        label_key="btn_shop_kind_custom",
                    )
                ],
                [
                    _ikb(
                        _label_or(ui, "btn_shop_kind_trial", "تست"),
                        callback_data="adm:plans:add:users:trial",
                        ui=ui,
                        label_key="btn_shop_kind_trial",
                    )
                ],
                [
                    _ikb(
                        wholesale_label,
                        callback_data="adm:plans:add:users:wholesale",
                        ui=ui,
                        label_key="btn_wholesale",
                    )
                ],
            ]
        )
    rows = chunk_buttons([row[0] for row in rows], cols=2)
    rows.append([back_button(ui, f"adm:plans:aud:{back_aud}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_plans_list_keyboard(
    plans: list,
    ui: dict | None = None,
    *,
    back_callback: str = "adm:plans:aud:users",
    add_callback: str = "adm:plan:add",
    kind: str = "fixed",
    pager: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """User fixed-plan rows — per-plan color + inline add/back; ``pager`` pages a long list."""
    from app.services.button_styles import plan_kind_style_id

    kind_style = _style(
        ui, plan_kind_style_id(kind) or "shop_kind_fixed", fallback="primary"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for p in [p for p in plans if not getattr(p, "is_trial", False)][:LIST_BUTTON_CAP]:
        warn = ""
        if not getattr(p, "pg_template_id", None) and not (getattr(p, "pg_group_ids", None) or "").strip():
            warn = " ⚠️"
        rows.append(
            [
                _ikb(
                    f"{'✅' if p.is_active else '⏸'} #{p.id} {p.name}{warn}"[:60],
                    callback_data=f"adm:plan:view:{p.id}",
                    style=_plan_row_style(ui, p, kind=kind),
                )
            ]
        )
    if not rows:
        rows.append(
            [
                _ikb(
                    feedback.NO_PLANS,
                    callback_data="adm:plans:noop",
                    style=kind_style,
                )
            ]
        )
    if pager:
        rows.append(pager)
    rows.append(_add_plan_row(ui, add_callback))
    if back_callback:
        rows.append(
            [
                back_button(ui, back_callback)
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_reseller_plans_list_keyboard(
    plans: list,
    ui: dict | None = None,
    *,
    mode: str,
    back_callback: str = "adm:plans:aud:resellers",
    add_callback: str = "adm:resplan:add:fixed",
    pager: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """Platform reseller subscription / addon packs — per-plan color; ``pager`` pages a long list."""
    mode_key = (mode or "fixed").strip().lower()
    kind_style = _style(
        ui,
        "plan_res_payg" if mode_key == "payg" else "plan_res_fixed",
        fallback="primary",
    )
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans[:LIST_BUTTON_CAP]:
        flag = "✅" if getattr(p, "is_active", True) else "⏸"
        name = (getattr(p, "name", "") or "")[:22]
        if mode_key == "payg":
            rate = int(getattr(p, "price_per_gb", 0) or 0)
            extra = f" · {rate:,} ت/گیگ".replace(",", "٬") if rate else ""
        elif mode_key == "addon_volume":
            qty = int(getattr(p, "addon_gb", 0) or 0)
            extra = f" · +{qty} گیگ"
        elif mode_key == "addon_users":
            qty = int(getattr(p, "addon_users", 0) or 0)
            extra = f" · +{qty} کاربر"
        else:
            extra = ""
        rows.append(
            [
                _ikb(
                    f"{flag} {name}{extra}"[:60],
                    callback_data=f"adm:resplan:view:{p.id}",
                    style=_plan_row_style(ui, p, kind=mode_key, audience="resellers"),
                )
            ]
        )
    if not rows:
        rows.append(
            [
                _ikb(
                    feedback.NO_PLANS,
                    callback_data="adm:plans:noop",
                    style=kind_style,
                )
            ]
        )
    if pager:
        rows.append(pager)
    rows.append(_add_plan_row(ui, add_callback))
    if back_callback:
        rows.append(
            [
                back_button(ui, back_callback)
            ]
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


def admin_reseller_actions(
    user_id: int,
    *,
    has_shop_services: bool = False,
    color_tag: str | None = None,
    oversell: bool | None = None,
) -> InlineKeyboardMarkup:
    """Reseller card actions — PG-owned services are primary; optional shop UserServices link.

    ``oversell`` is the volume-oversell switch; None hides it (PAYG shops have no ceiling).
    """
    from app.services.color_tags import bot_tag_button_label

    tag_label = bot_tag_button_label(color_tag)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📦 سرویس‌های پاسارگارد",
                callback_data=f"adm:resellers:svcs:{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="✉️ پیام",
                callback_data=f"adm:resellers:msg:{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⏱ تغییر ظرفیت",
                callback_data=f"adm:resellers:capadj:{user_id}",
            )
        ],
        # One button per row: pack_detail_rows pairs them two by two.
        [InlineKeyboardButton(text="➕ افزایش کیف", callback_data=f"adm:resellers:wcredit:{user_id}")],
        [InlineKeyboardButton(text="➖ کاهش کیف", callback_data=f"adm:resellers:wdebit:{user_id}")],
        [
            InlineKeyboardButton(
                text=f"🏷 تگ: {tag_label}"[:64],
                callback_data=f"adm:resellers:tag:{user_id}",
            )
        ],
    ]
    if oversell is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📈 فروش بیش از سقف حجم: {'روشن' if oversell else 'خاموش'}",
                    callback_data=f"adm:resellers:osell:{user_id}",
                )
            ]
        )
    if has_shop_services:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛒 سرویس‌های فروشگاه",
                    callback_data=f"adm:users:svcs:{user_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🤝 حذف نمایندگی",
                callback_data=f"adm:users:unres:{user_id}",
            )
        ]
    )
    rows.append(
        [back_button(None, "adm:resellers:list:0")]
    )
    return InlineKeyboardMarkup(inline_keyboard=pack_detail_rows(rows))


def admin_reseller_risk_tag_keyboard(
    user_id: int, *, current: str | None = None
) -> InlineKeyboardMarkup:
    """Inline picker for the 4 staff risk-color levels on a reseller card."""
    from app.services.color_tags import COLOR_TAGS, normalize_color_tag

    cur = normalize_color_tag(current)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in COLOR_TAGS:
        mark = "✓ " if t.key == cur else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{t.emoji} {t.title_fa}"[:64],
                callback_data=f"adm:resellers:tagset:{user_id}:{t.key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            back_button(None, f"adm:resellers:view:{user_id}")
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_reseller_capacity_adjust_keyboard(
    user_id: int,
    *,
    days: int = 0,
    gb: int = 0,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➖",
                    callback_data=f"adm:resellers:capadj:{user_id}:days:-",
                ),
                InlineKeyboardButton(
                    text=f"{days} روز",
                    callback_data=f"adm:resellers:capadj:{user_id}:days:input",
                ),
                InlineKeyboardButton(
                    text="➕",
                    callback_data=f"adm:resellers:capadj:{user_id}:days:+",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➖",
                    callback_data=f"adm:resellers:capadj:{user_id}:gb:-",
                ),
                InlineKeyboardButton(
                    text=f"{gb} گیگ",
                    callback_data=f"adm:resellers:capadj:{user_id}:gb:input",
                ),
                InlineKeyboardButton(
                    text="➕",
                    callback_data=f"adm:resellers:capadj:{user_id}:gb:+",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ اعمال تغییر",
                    callback_data=f"adm:resellers:capadj:{user_id}:confirm",
                )
            ],
            [
                back_button(None, f"adm:resellers:view:{user_id}")
            ],
        ]
    )


def admin_reseller_services_keyboard(
    reseller_id: int,
    *,
    page: int,
    has_prev: bool,
    has_next: bool,
    pg_users: list[dict],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    for u in pg_users:
        uid = u.get("id")
        if uid is None:
            continue
        uname = str(u.get("username") or f"#{uid}")[:28]
        status = str(u.get("status") or "")
        mark = "⛔ " if status in {"disabled", "limited", "expired"} else (
            "⏸ " if status == "on_hold" else ""
        )
        buttons.append(
            InlineKeyboardButton(
                text=f"{mark}{uname}"[:48],
                callback_data=f"adm:pg:u:{int(uid)}",
            )
        )
    rows = chunk_buttons(buttons, cols=2)
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(
            InlineKeyboardButton(
                text="◀️ قبل",
                callback_data=f"adm:resellers:svcs:{reseller_id}:{page - 1}",
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="بعد ▶️",
                callback_data=f"adm:resellers:svcs:{reseller_id}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            back_button(None, f"adm:resellers:view:{reseller_id}")
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_actions(
    user_id: int,
    *,
    is_blocked: bool,
    role: str | None = None,
    confirm_delete: bool = False,
    ui: dict | None = None,
    has_services: bool = False,
    color_tag: str | None = None,
) -> InlineKeyboardMarkup:
    from app.services.color_tags import bot_tag_button_label

    block_label = "🔓 رفع مسدودی" if is_blocked else "🚫 مسدود کردن"
    tag_label = bot_tag_button_label(color_tag)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ افزایش کیف", callback_data=f"adm:users:wcredit:{user_id}"
            ),
            InlineKeyboardButton(
                text="➖ کاهش کیف", callback_data=f"adm:users:wdebit:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📦 سرویس‌ها", callback_data=f"adm:users:svcs:{user_id}"
            ),
            InlineKeyboardButton(
                text=f"🏷 تگ: {tag_label}"[:64],
                callback_data=f"adm:users:tag:{user_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✉️ پیام", callback_data=f"adm:users:msg:{user_id}"
            ),
            InlineKeyboardButton(text=block_label, callback_data=f"adm:users:block:{user_id}"),
        ],
    ]
    if has_services:
        rows.insert(
            1,
            [
                InlineKeyboardButton(
                    text="🔄 تمدید سریع",
                    callback_data=f"adm:users:renew:{user_id}",
                )
            ],
        )
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
                _ikb(
                    "⚠️ ادامه — نوشتن علت حذف",
                    callback_data=f"adm:users:del:{user_id}",
                    style=_style(ui, "reject", fallback="danger"),
                )
            ]
        )
        rows.append(
            [
                _ikb(
                    "⬅️ انصراف",
                    callback_data=f"adm:users:view:{user_id}",
                    style=_style(ui, "cancel", fallback="danger"),
                )
            ]
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
    rows.append(
        [
            back_button(ui, "adm:users:list:0")
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_risk_tag_keyboard(user_id: int, *, current: str | None = None) -> InlineKeyboardMarkup:
    """Inline picker for the 4 staff risk-color levels."""
    from app.services.color_tags import COLOR_TAGS, normalize_color_tag

    cur = normalize_color_tag(current)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in COLOR_TAGS:
        mark = "✓ " if t.key == cur else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{t.emoji} {t.title_fa}"[:64],
                callback_data=f"adm:users:tagset:{user_id}:{t.key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [back_button(None, f"adm:users:view:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_services_keyboard(
    user_id: int, service_ids: list[int]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for sid in service_ids[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"سرویس #{sid}",
                    callback_data=f"adm:users:svc:{user_id}:{sid}",
                )
            ]
        )
    rows.append(
        [back_button(None, f"adm:users:view:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_service_actions(user_id: int, service_id: int) -> InlineKeyboardMarkup:
    ids = f"{user_id}:{service_id}"
    actions = [
        InlineKeyboardButton(text="🔗 لینک سرویس", callback_data=f"adm:users:svclink:{ids}"),
        InlineKeyboardButton(text="🔄 تمدید با پلن فعلی", callback_data=f"adm:users:svcrenew:{ids}"),
        InlineKeyboardButton(text="⏱ تغییر مانده", callback_data=f"adm:users:svcadj:{ids}"),
        InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"adm:users:svcdelask:{ids}"),
    ]
    rows = chunk_buttons(actions, cols=2)
    rows.append(
        [back_button(None, f"adm:users:svcs:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_service_delete_confirm(
    user_id: int, service_id: int, *, external: bool = False
) -> InlineKeyboardMarkup:
    cancel = InlineKeyboardButton(
        text="انصراف", callback_data=f"adm:users:svc:{user_id}:{service_id}"
    )
    if external:
        # Added from outside the bot: detaching is separate from removing the panel account.
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔗 فقط جدا شود (حساب پنل می‌ماند)",
                        callback_data=f"adm:users:svcdel:{user_id}:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 حذف همراه حساب پاسارگارد",
                        callback_data=f"adm:users:svcdel:{user_id}:{service_id}:pg",
                    )
                ],
                [cancel],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف شود",
                    callback_data=f"adm:users:svcdel:{user_id}:{service_id}",
                ),
                cancel,
            ]
        ]
    )


def admin_user_service_adjust_keyboard(
    user_id: int,
    service_id: int,
    *,
    days: int = 0,
    gb: float = 0,
) -> InlineKeyboardMarkup:
    """Interactive signed adjust: − | N | + for days and GB."""
    gb_disp = int(gb) if float(gb) == int(gb) else gb
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➖",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:days:-",
                ),
                InlineKeyboardButton(
                    text=f"{days} روز",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:days:input",
                ),
                InlineKeyboardButton(
                    text="➕",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:days:+",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➖",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:gb:-",
                ),
                InlineKeyboardButton(
                    text=f"{gb_disp} گیگ",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:gb:input",
                ),
                InlineKeyboardButton(
                    text="➕",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:gb:+",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ اعمال تغییر",
                    callback_data=f"adm:users:svcadj:{user_id}:{service_id}:confirm",
                )
            ],
            [
                back_button(None, f"adm:users:svc:{user_id}:{service_id}")
            ],
        ]
    )


# Review action labels (order / payment / reseller-app) — reply keyboard
REPLY_ACTION_REV_OK = "rev_ok"
REPLY_ACTION_REV_NO = "rev_no"
BTN_REV_OK = "🟢✅ تأیید"
BTN_REV_NO = "🔴❌ رد"


def order_review(order_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Message-scoped approve/reject (notifications). No nav chrome."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    "🟢✅ تأیید سفارش",
                    callback_data=f"ordrev:ok:{order_id}",
                    style=_style(ui, "confirm", fallback="success"),
                ),
                _ikb(
                    "🔴❌ رد",
                    callback_data=f"ordrev:no:{order_id}",
                    style=_style(ui, "reject", fallback="danger"),
                ),
            ],
        ]
    )


def payment_review(payment_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Pending payment: approve (ok) + reject. No nav chrome."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    "🟢✅ تأیید",
                    callback_data=f"payrev:ok:{payment_id}",
                    style=_style(ui, "confirm", fallback="success"),
                ),
                _ikb(
                    "🔴❌ رد",
                    callback_data=f"payrev:no:{payment_id}",
                    style=_style(ui, "reject", fallback="danger"),
                ),
            ]
        ]
    )


def payment_resume(payment_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """APPROVED but fulfill/credit incomplete — resume only (never reject)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    "♻️ ادامه تحویل",
                    callback_data=f"payrev:go:{payment_id}",
                    style=_style(ui, "confirm", fallback="success"),
                ),
            ]
        ]
    )


def payment_resend(payment_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Service ready; Telegram send failed — resend message only."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ikb(
                    "📤 ارسال مجدد پیام",
                    callback_data=f"payrev:send:{payment_id}",
                    style=_style(ui, "confirm", fallback="success"),
                ),
            ]
        ]
    )


def payment_action_markup(
    payment_id: int,
    *,
    action: str,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Build Phase 1 markup for ``approve`` / ``resume`` / ``resend``."""
    if action == "resume":
        return payment_resume(payment_id, ui)
    if action == "resend":
        return payment_resend(payment_id, ui)
    return payment_review(payment_id, ui)


def reseller_app_review(
    app_id: int,
    ui: dict | None = None,
    *,
    allow_approve: bool = True,
) -> InlineKeyboardMarkup:
    """Message-scoped approve/reject (notifications). No nav chrome.

    ``allow_approve=False`` for unpaid (pending_payment) apps — reject only.
    """
    row: list[InlineKeyboardButton] = []
    if allow_approve:
        row.append(
            _ikb(
                "🟢✅ تأیید",
                callback_data=f"adm:resapp:ok:{app_id}",
                style=_style(ui, "confirm", fallback="success"),
            )
        )
    row.append(
        _ikb(
            "🔴❌ رد",
            callback_data=f"adm:resapp:no:{app_id}",
            style=_style(ui, "reject", fallback="danger"),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def reseller_home(profile=None, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Legacy stub — navigation is on reseller_reply_keyboard."""
    _ = profile, ui
    return InlineKeyboardMarkup(inline_keyboard=[])


def back_home(ui: dict | None = None) -> InlineKeyboardMarkup:
    """One «🏠 منوی اصلی» button, for screens that are the end of a path (nothing to go back to)."""
    return InlineKeyboardMarkup(inline_keyboard=[[home_button(ui)]])


def home_button(ui: dict | None = None) -> InlineKeyboardButton:
    return _ikb(_home_label(ui), callback_data="menu:home", ui=ui, label_key="btn_menu_home", style_id="back")


BTN_CANCEL = "انصراف"
BTN_RESTART = "🏠 شروع مجدد"  # legacy alias only — no longer shown on cancel KB


def is_cancel_text(text: str | None) -> bool:
    t = (text or "").strip()
    return t in {BTN_CANCEL, "لغو", "cancel", "/cancel"} or t.endswith("انصراف")


def is_home_text(text: str | None, ui: dict | None = None) -> bool:
    """True for main-menu / legacy restart labels."""
    t = (text or "").strip()
    if not t:
        return False
    home = _home_label(ui)
    if t == home or t == (_t(ui, "btn_restart") or "").strip():
        return True
    return t in {BTN_RESTART, "شروع مجدد", "restart", "🏠 منوی اصلی"} or t.endswith(
        "شروع مجدد"
    ) or t.endswith("منوی اصلی")


def is_restart_text(text: str | None) -> bool:
    """Backward-compat alias — treats home + legacy restart as home navigation."""
    return is_home_text(text)
