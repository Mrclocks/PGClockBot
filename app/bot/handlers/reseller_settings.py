"""Reseller shop settings in Telegram — subset of admin settings, scoped to their bot."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import delete_user_message, finish_text_step
from app.bot.secret_fields import SECRET, is_secret, secret_prompt, secret_state
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.user_errors import domain_error_text
from app.services.connection_status_texts import CONNECTION_STATUS_MENU_KEY
from app.services.trial_offer import TRIAL_MENU_KEY
from app.bot.entry_gate import ENTRY_APPROVAL_BOT_FIELDS, ENTRY_APPROVAL_SUB, ENTRY_APPROVAL_SUB_TITLE
from app.services.entry_approval_config import PERMISSION as ENTRY_APPROVAL_PERMISSION
from app.services.resellers import get_reseller_profile, has_bot_perm
from app.services.support_contacts import (
    delete_support_contact,
    get_support_contacts,
    upsert_support_contact,
)
from app.services.bbc_settlement import ENABLED_KEY as BBC_ENABLED_KEY, bbc_enable_error
from app.services.bot_appearance import PROFILE_TEXT_FIELDS, update_bot_profile_text
from app.services.secret_box import reveal_bot_token
from app.services.receipt_fingerprints import normalize_dup_policy
from app.services.rich_text import pack_setting_from_message
from app.services.users import (
    get_all_settings,
    get_setting,
    normalize_force_join_channel_value,
    on,
    reset_shop_reseller_id,
    set_setting,
    set_shop_reseller_id,
)
from app.services.settings_button_labels import (
    BTN_PAY_CARD,
    BTN_PAY_CRYPTO,
    BTN_PAY_GATEWAY,
    BTN_PAY_BBC,
    BTN_PAY_PSP,
    BTN_PAY_STARS,
    BTN_PAY_WALLET_DISCOUNT,
    button_label_subs,
)
from app.services.bot_setting_values import (
    NUMBER_UNLIMITED_VALUES,
    SELECT_OPTIONS,
    UNLIMITED_LABEL,
    VALUE_NORMALIZERS,
    next_select_value,
    number_range_hint,
    parse_number_setting,
    select_label,
    text_template_hint,
    wants_default_text,
)
from app.services.service_import_config import (
    KEY_ENABLED as IMPORT_KEY_ENABLED,
    KEY_MAX_PER_USER as IMPORT_KEY_MAX_PER_USER,
    KEY_MODE as IMPORT_KEY_MODE,
)
from app.services.customer_notice_texts import CLEANUP_BOT_FIELDS, STAFF_ACTION_BOT_FIELDS
from app.services.settings_message_texts import (
    CONNECTION_STATUS_CUSTOMER_TEXTS,
    MESSAGE_TEXT_SUBS,
    PROFILE_BOT_FIELDS,
)
from app.services.receipt_risk import BOT_FIELDS as RECEIPT_RISK_BOT_FIELDS
from app.services.service_freeze_settings import BOT_FIELDS as SERVICE_FREEZE_BOT_FIELDS, GROUP_TITLE as SERVICE_FREEZE_GROUP
from app.services.service_transfer_settings import (
    BOT_FIELDS as SERVICE_TRANSFER_BOT_FIELDS,
    GROUP_TITLE as SERVICE_TRANSFER_GROUP,
)
from app.services.settings_operation_fields import (
    AUTO_APPROVE_AFTER_FIELDS,
    AUTO_RENEW_FIELDS,
    CRYPTO_RATE_FIELDS,
    RECEIPT_MATCH_FIELDS,
    SALES_STATE_FIELDS,
    STARS_FIELDS,
    TRIAL_MIN_AGE_FIELD,
    USAGE_ALERT_FIELDS,
    WALLET_TOPUP_FIELDS,
)
from app.services import bot_feedback_texts as feedback
from app.services.screen_titles import screen_caption, screen_heading, with_caption
from app.bot.handlers.button_colors import COLORS_SECTION
from app.bot.handlers.button_colors import entry_button as colors_entry_button
from app.bot.handlers.guide_editor import GUIDES_SECTION
from app.bot.handlers.payment_destinations_editor import destination_list_rows
from app.bot.plan_scope_picker import (
    FIELD_KIND as PLAN_SCOPE_KIND,
    picker_markup as plan_picker_markup,
    picker_text as plan_picker_text,
    scope_button as plan_scope_button,
)
from app.services.plan_scope import plan_choices, toggle_plan_scope
from app.bot.handlers.settings_labels import (
    LABELS_SECTION,
    labels_audience_panel,
    labels_home_panel,
    resolve_sub,
    sub_back,
)
from app.bot.handlers.guide_editor import entry_button as guides_entry_button
from app.bot.handlers.screen_editor import entry_button as editor_entry_button
from app.services.screen_editor import is_editor_enabled
import html
import logging

logger = logging.getLogger(__name__)




router = Router(name="reseller_settings")

Field = tuple[str, str, str]

SUB_PERMISSIONS: dict[str, str] = {ENTRY_APPROVAL_SUB: ENTRY_APPROVAL_PERMISSION}

SECTIONS: dict[str, dict] = {
    # Same IA as owner hub — shop-scoped only (no platform keys).
    "shop": {
        "title": "نام و متن‌ها",
        "subs": [
            ("identity", "نام و خوش‌آمد", [
                ("shop_title", "نام فروشگاه", "text"),
                ("welcome_text", "پیام /start", "textarea"),
            ]),
            ("texts", "متن‌های ضروری", [
                ("support_text", "متن پشتیبانی", "textarea"),
                ("purchase_success_text", "موفقیت خرید", "textarea"),
                ("payment_reject_text", "رد پرداخت", "textarea"),
            ]),
            ("invite", "دعوت دوستان", [
                ("referral_text", "متن دعوت", "textarea"),
                ("referral_bonus", "پاداش دعوت", "number"),
            ]),
            ("profile", "پروفایل تلگرام ربات", PROFILE_BOT_FIELDS),
            *MESSAGE_TEXT_SUBS,
            ("status_texts", "متن‌های وضعیت اتصال", CONNECTION_STATUS_CUSTOMER_TEXTS),
        ],
    },
    "menu": {
        "title": "ظاهر منو",
        "subs": [
            ("layout", "چیدمان کیبورد", "menu_layout"),
            ("order", "ترتیب دکمه‌ها", "menu_order"),
        ],
    },
    LABELS_SECTION: {
        "title": "متن دکمه‌ها",
        "kind": "labels",
        "subs": button_label_subs(include_platform=False),
    },
    "pay": {
        "title": "پرداخت",
        "subs": [
            ("methods", "روش‌های فعال", [
                ("pay_wallet_enabled", "کیف پول", "toggle"),
                ("pay_card_enabled", "کارت به کارت", "toggle"),
                ("pay_card_auto_enabled", "تأیید خودکار کارت به کارت", "toggle"),
                ("pay_gateway_enabled", "درگاه لینک", "toggle"),
                ("pay_psp_enabled", "درگاه API", "toggle"),
                ("pay_bbc_enabled", "بزن به کارت (bezanbecard.ir)", "toggle"),
                ("pay_crypto_enabled", "رمزارز", "toggle"),
                ("pay_stars_enabled", "استارز", "toggle"),
                ("pay_discount_enabled", "کد تخفیف", "toggle"),
                ("auto_approve_payments", "تأیید خودکار رسید", "toggle"),
            ]),
            ("pay_btns", "متن دکمه‌های پرداخت مشترک", [
                *BTN_PAY_WALLET_DISCOUNT,
                *BTN_PAY_STARS,
            ]),
            ("card", "کارت به کارت", [
                ("card_number", "شماره کارت", "text"),
                ("card_holder", "صاحب کارت", "text"),
                ("payment_cards_random", "نمایش کارت تصادفی", "toggle"),
                ("card_pay_text", "راهنمای پرداخت", "textarea"),
                *BTN_PAY_CARD,
            ]),
            ("card_auto", "تأیید خودکار کارت به کارت", [
                ("card_auto_provider", "ارائه‌دهنده", "text"),
                ("card_auto_webhook_secret", "رمز وب‌هوک", SECRET),
                ("card_auto_hint_text", "راهنما", "textarea"),
            ]),
            ("gateway", "درگاه لینک", [
                ("gateway_name", "نام درگاه", "text"),
                ("gateway_link", "لینک", "text"),
                ("gateway_pay_text", "راهنما", "textarea"),
                *BTN_PAY_GATEWAY,
            ]),
            ("psp", "درگاه API", [
                ("psp_provider", "ارائه‌دهنده (zarinpal/sunpay/mock)", "text"),
                ("psp_merchant_id", "مرچنت / کلید API", SECRET),
                ("psp_pay_url", "آدرس پایه سان‌پی (PAY_URL)", "text"),
                ("psp_sunpay_request_method", "روش /api/request سان‌پی (GET/POST)", "text"),
                ("psp_checkout_link_mode", "لینک دکمهٔ درگاه (direct/panel)", "text"),
                ("psp_sandbox", "سندباکس", "toggle"),
                ("psp_pay_text", "راهنما", "textarea"),
                *BTN_PAY_PSP,
            ]),
            ("bbc", "بزن به کارت", [
                ("bbc_api_key", "کلید API (bezanbecard.ir)", SECRET),
                ("bbc_pay_text", "راهنمای مشتری", "textarea"),
                *BTN_PAY_BBC,
            ]),
            ("crypto", "رمزارز", [
                ("crypto_asset", "رمزارز", "text"),
                ("crypto_network", "شبکه", "text"),
                ("crypto_address", "آدرس ولت", "text"),
                ("crypto_pay_text", "راهنما", "textarea"),
                *BTN_PAY_CRYPTO,
            ]),
            ("stars", "استارز", STARS_FIELDS),
            ("auto_after", "تأیید خودکار با مهلت", AUTO_APPROVE_AFTER_FIELDS),
            ("crypto_rate", "نرخ تبدیل رمزارز", CRYPTO_RATE_FIELDS),
            ("wallet_topup", "شارژ کیف پول", WALLET_TOPUP_FIELDS),
        ],
    },
    "support": {
        "title": "پشتیبان‌ها",
        "kind": "supports",
    },
    "access": {
        "title": "تحویل سرویس",
        "subs": [
            ("qr", "QR اشتراک", [
                ("qr_enabled", "ارسال خودکار QR", "toggle"),
                ("show_sub_link_in_text", "لینک در کپشن", "toggle"),
                ("qr_caption", "کپشن QR", "textarea"),
            ]),
            # Same keys as the web «افزودن سرویس قبلی» tab; paths and panel admins stay with the Owner.
            ("import", "افزودن سرویس قبلی", [
                (IMPORT_KEY_ENABLED, "افزودن سرویس قبلی توسط مشتری", "toggle"),
                (IMPORT_KEY_MODE, "روش پذیرش", "select"),
                (IMPORT_KEY_MAX_PER_USER, "سقف سرویس هر مشتری", "number"),
            ]),
            ("freeze", SERVICE_FREEZE_GROUP, SERVICE_FREEZE_BOT_FIELDS),
            ("transfer", SERVICE_TRANSFER_GROUP, SERVICE_TRANSFER_BOT_FIELDS),
            ("naming", "نام در پاسارگارد", [
                ("pg_username_prefix", "پیشوند", "text"),
                ("pg_username_suffix", "پسوند", "text"),
                ("pg_username_pattern", "الگو", "text"),
            ]),
        ],
    },
    "limits": {
        "title": "ورود و عضویت",
        "subs": [
            ("force", "کانال اجباری", [
                ("force_join_enabled", "فعال", "toggle"),
                ("force_join_channel", "کانال‌ها (هر خط یکی)", "text"),
                ("force_join_msg", "متن پیام عضویت", "textarea"),
                ("btn_force_join", "متن دکمه لینک کانال", "text"),
                ("btn_force_join_check", "متن دکمه بررسی", "text"),
            ]),
            ("terms", "قوانین ورود", [
                ("terms_entry_enabled", "فعال ورود", "toggle"),
                ("terms_entry_text", "متن قوانین ورود", "textarea"),
                ("terms_entry_btn", "دکمه موافقت ورود", "text"),
                ("terms_entry_reaccept", "پذیرش مجدد ورود", "toggle"),
            ]),
            ("referral", "معرف اجباری", [
                ("referral_required", "معرف اجباری", "toggle"),
                ("referral_required_text", "پیام دریافت معرف", "textarea"),
            ]),
            (ENTRY_APPROVAL_SUB, ENTRY_APPROVAL_SUB_TITLE, list(ENTRY_APPROVAL_BOT_FIELDS)),
        ],
    },
    "buy": {
        "title": "قوانین خرید",
        "subs": [
            ("trial_gate", "سرویس تست", [
                ("trial_require_contact", "تأیید شماره تماس", "toggle"),
                ("trial_require_iran_phone", "فقط شماره ایران", "toggle"),
                TRIAL_MIN_AGE_FIELD,
            ]),
            ("sales", "وضعیت فروش", SALES_STATE_FIELDS),
            ("purchase_contact", "شماره تماس خرید", [
                ("purchase_require_contact", "شماره تماس اجباری", "toggle"),
            ]),
            ("receipt_dup", "ضدتقلب رسید", [
                ("receipt_dup_policy", "رسید تکراری (warn/block)", "text"),
                *RECEIPT_RISK_BOT_FIELDS,
            ]),
            ("receipt_match", "تطبیق رسید", RECEIPT_MATCH_FIELDS),
            ("terms_buy", "قوانین خرید", [
                ("terms_buy_user_enabled", "فعال خرید کاربر", "toggle"),
                ("terms_buy_user_text", "متن قوانین خرید کاربر", "textarea"),
                ("terms_buy_user_btn", "دکمه موافقت خرید کاربر", "text"),
                ("terms_buy_user_reaccept", "پذیرش مجدد خرید کاربر", "toggle"),
                ("terms_buy_reseller_enabled", "فعال خرید نماینده", "toggle"),
                ("terms_buy_reseller_text", "متن قوانین خرید نماینده", "textarea"),
                ("terms_buy_reseller_btn", "دکمه موافقت خرید نماینده", "text"),
                ("terms_buy_reseller_reaccept", "پذیرش مجدد خرید نماینده", "toggle"),
            ]),
        ],
    },
    "guides": {
        "title": "آموزش اتصال",
        "subs": [
            ("labels", "متن دکمه‌ها", [
                ("btn_guides", "دکمه فهرست آموزش", "text"),
                ("btn_guide_open_link", "دکمه لینک اشتراک", "text"),
            ]),
        ],
    },
    "notify": {
        "title": "اعلان‌ها",
        "kind": "notify",
        # Opened from the notify screen; the report text and metrics stay on the web (placeholder picker).
        "subs": [
            ("daily", "گزارش روزانه", [
                ("admin_daily_report_enabled", "ارسال گزارش روزانه", "toggle"),
                ("subordinate_report_enabled", "گزارش نمایندگان زیرمجموعه", "toggle"),
                ("admin_daily_report_hour", "ساعت ارسال (۰ تا ۲۳، به وقت تهران)", "number"),
            ]),
            ("usage_alerts", "هشدار سرویس کاربر", USAGE_ALERT_FIELDS),
            ("auto_renew", "تمدید خودکار سرویس", AUTO_RENEW_FIELDS),
            ("staff_notices", "متن اعلان کار روی سرویس", STAFF_ACTION_BOT_FIELDS),
            ("cleanup_notices", "متن اعلان پاکسازی سرویس", CLEANUP_BOT_FIELDS),
        ],
    },
    # Own shop bot token only — never platform BOT_TOKEN.
    "bot": {
        "title": "ربات",
        "kind": "bot",
    },
}

HUB_ORDER = ["shop", "menu", LABELS_SECTION, "pay", "access", "limits", "buy", "support", "guides", "notify", "bot"]


class ResellerSettingsStates(StatesGroup):
    edit_value = State()
    support_title = State()
    support_telegram = State()
    bot_token = State()


def _field_lookup() -> dict[str, Field]:
    out: dict[str, Field] = {}
    for sec in SECTIONS.values():
        for sub in sec.get("subs") or []:
            if isinstance(sub[2], list):
                for f in sub[2]:
                    out[f[0]] = f
    return out


FIELDS = _field_lookup()


def _is_field_toggle_key(key: str) -> bool:
    meta = FIELDS.get(key)
    return bool(meta and meta[2] == "toggle")


def _preview(value: str | None, *, limit: int = 120) -> str:
    from app.services.rich_text import rich_plain_text

    text = rich_plain_text(value).strip()
    if not text:
        return "—"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


MENU_ORDER_LABELS = {
    "shop": "خرید",
    TRIAL_MENU_KEY: "دریافت تست",
    "services": "سرویس‌ها",
    CONNECTION_STATUS_MENU_KEY: "وضعیت اتصال",
    "wallet": "کیف پول",
    "support": "پشتیبانی",
    "loyalty": "باشگاه مشتریان",
    "referral": "باشگاه مشتریان",
}


async def _render_menu_order(
    callback: CallbackQuery,
    session: AsyncSession,
    reseller_id: int,
    *,
    back: str,
) -> None:
    from app.bot.keyboards import DEFAULT_MENU_ORDER

    with _Scoped(reseller_id):
        raw = await get_setting(session, "menu_order", reseller_id=reseller_id) or ""
    order = [
        p.strip()
        for p in raw.split(",")
        if p.strip() and p.strip() in DEFAULT_MENU_ORDER and p.strip() not in {"reseller_apply", "miniapp"}
    ]
    if "shop" not in order:
        order.insert(0, "shop")
    rows: list[list[InlineKeyboardButton]] = []
    for i, key in enumerate(order[:10]):
        label = MENU_ORDER_LABELS.get(key, key)
        row = [InlineKeyboardButton(text=f"{i + 1}. {label}", callback_data="res:st:noop")]
        if i > 0:
            row.append(InlineKeyboardButton(text="⬆️", callback_data=f"res:st:menu:up:{i}"))
        if i < len(order) - 1:
            row.append(InlineKeyboardButton(text="⬇️", callback_data=f"res:st:menu:dn:{i}"))
        rows.append(row)
    # Offer pool items to add
    pool = [
        k
        for k in DEFAULT_MENU_ORDER
        if k not in order and k not in {"reseller_apply", "miniapp"}
    ]
    for key in pool[:6]:
        label = MENU_ORDER_LABELS.get(key, key)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ {label}",
                    callback_data=f"res:st:menu:add:{key}",
                )
            ]
        )
    # Removable (non-required)
    for key in order:
        if key == "shop":
            continue
        label = MENU_ORDER_LABELS.get(key, key)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 حذف {label}",
                    callback_data=f"res:st:menu:rm:{key}",
                )
            ]
        )
    rows.append([kb.back_button(None, back)])
    if callback.message:
        await safe_edit_text(
            callback.message,
            "<b>منوی فعال</b>\nترتیب را با فلش عوض کنید؛ از وب‌پنل هم می‌توانید کامل‌تر مدیریت کنید.",
            reply_markup=_kb(rows),
        )


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _gate(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_access import load_reseller_actor

    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        return None, feedback.RESELLERS_ONLY
    if not has_bot_perm(profile, "shop_settings"):
        return None, "دسترسی تنظیمات فروشگاه ندارید"
    return profile, None


class _Scoped:
    """Temporarily force reseller setting scope even on the main bot."""

    def __init__(self, reseller_user_id: int):
        self.reseller_user_id = reseller_user_id
        self._token = None

    def __enter__(self):
        self._token = set_shop_reseller_id(self.reseller_user_id)
        return self

    def __exit__(self, *args):
        if self._token is not None:
            reset_shop_reseller_id(self._token)


Panel = tuple[str, InlineKeyboardMarkup]
HubPanel = Panel


async def _hub_panel(session: AsyncSession, profile) -> HubPanel:
    ui = await get_all_settings(session, reseller_id=profile.user_id)
    bot_line = f"@{profile.bot_username}" if profile.bot_username else feedback.BOT_TOKEN_NOT_SET
    text = with_caption(
        screen_heading(ui, "r.settings"), f"ربات: <code>{bot_line}</code>", screen_caption(ui, "r.settings")
    )
    # nav_inline imports this package's helpers; defer to avoid an import cycle.
    from app.bot.nav_inline import reseller_settings_hub_keyboard
    from app.services.resellers import get_reseller_panel_base_url

    rows = list(reseller_settings_hub_keyboard(ui).inline_keyboard)
    base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
    if base.startswith("http"):
        rows.insert(-1, [InlineKeyboardButton(text="🌐 تنظیمات کامل در وب‌پنل", url=f"{base}/shop-settings")])
    return text, _kb(rows)


async def _close_input(
    message: Message,
    state: FSMContext,
    *,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Turn the input prompt into the next panel (same bubble) and drop the typed reply."""
    await finish_text_step(message, state, text=text, inline=markup)
    await state.clear()
    await delete_user_message(message)


async def _render_hub(
    callback: CallbackQuery,
    session: AsyncSession,
    profile,
    db_user: BotUser | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """The shop settings hub, edited in place."""
    text, markup = await _hub_panel(session, profile)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


async def _render_section(callback: CallbackQuery, session: AsyncSession, sec_id: str, reseller_id: int):
    sec = SECTIONS.get(sec_id)
    if not sec:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if sec.get("kind") == "notify":
        await _render_notify(callback, session, reseller_id)
        return
    panel = await _section_panel(session, sec_id, reseller_id)
    if panel is not None and callback.message:
        await safe_edit_text(callback.message, panel[0], reply_markup=panel[1])


async def _subs_without_permission(session: AsyncSession, reseller_id: int) -> set[str]:
    """Subsections the shop's package does not include (nobody there could act on them)."""
    profile = await get_reseller_profile(session, reseller_id)
    return {sub for sub, perm in SUB_PERMISSIONS.items() if not has_bot_perm(profile, perm)}


async def _section_panel(session: AsyncSession, sec_id: str, reseller_id: int) -> Panel | None:
    """One settings section (supports list, bot token, or its subsections); None for unknown ids."""
    sec = SECTIONS.get(sec_id)
    if not sec or sec.get("kind") == "notify":
        return None
    if sec.get("kind") == "labels":
        return labels_home_panel("res", sec["subs"])
    with _Scoped(reseller_id):
        if sec.get("kind") == "supports":
            contacts = await get_support_contacts(session, reseller_id=reseller_id)
            rows = []
            for c in contacts:
                mark = "✅" if c.get("enabled") else "⏸"
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{mark} {c['title']}",
                            callback_data=f"res:st:sup:del:{c['id']}",
                        )
                    ]
                )
            rows.append(
                [InlineKeyboardButton(text="➕ پشتیبان جدید", callback_data="res:st:sup:add")]
            )
            rows.append([kb.back_button(None, "res:st:hub")])
            body = "پشتیبان فعال نیست." if not contacts else "برای حذف روی مورد بزنید."
            return f"💬 <b>پشتیبان‌ها</b>\n\n{body}", _kb(rows)

        if sec.get("kind") == "bot":
            profile = await get_reseller_profile(session, reseller_id)
            uname = f"@{profile.bot_username}" if profile and profile.bot_username else "—"
            rows = [
                [InlineKeyboardButton(text="🔄 تغییر توکن ربات", callback_data="res:st:bot:token")],
                [kb.back_button(None, "res:st:hub")],
            ]
            text = (
                "🤖 <b>ربات فروشگاه</b>\n\n"
                f"یوزرنیم: <code>{uname}</code>\n"
                "فقط توکن <b>همین فروشگاه</b> را از @BotFather بگیرید.\n"
                "توکن ربات اصلی/پلتفرم اینجا کار نمی‌کند و ذخیره نمی‌شود به‌عنوان پلتفرم.\n"
                "پس از تغییر، ربات ظرف چند ثانیه وصل می‌شود."
            )
            return text, _kb(rows)

        subs = sec.get("subs") or []
        # Only a section with a permission-bound subsection needs the shop's profile.
        gated = any(sub_id in SUB_PERMISSIONS for sub_id, *_ in subs)
        hidden = await _subs_without_permission(session, reseller_id) if gated else set()
        rows = kb.pack_long_list(
            [
                InlineKeyboardButton(text=label, callback_data=f"res:st:sub:{sec_id}:{sub_id}")
                for sub_id, label, _fields in subs
                if sub_id not in hidden
            ]
        )
        if sec_id == GUIDES_SECTION:
            rows.append([guides_entry_button(for_reseller=True)])
        if sec_id == COLORS_SECTION:
            rows.append([colors_entry_button(for_reseller=True)])
            # Shop owners can only edit while the platform owner has the editor enabled.
            if await is_editor_enabled(session):
                rows.append([editor_entry_button(for_reseller=True)])
        rows.append([kb.back_button(None, "res:st:hub")])
        return f"⚙️ <b>{sec['title']}</b>", _kb(rows)


async def _render_notify(callback: CallbackQuery, session: AsyncSession, reseller_id: int):
    from app.services.authz import resolve_shop_permissions_from_profile
    from app.services.notifications import get_shop_notify_prefs, shop_notify_catalog

    profile = await get_reseller_profile(session, reseller_id)
    perms = list(resolve_shop_permissions_from_profile(profile) or [])
    catalog = shop_notify_catalog(perms)
    prefs = await get_shop_notify_prefs(session, reseller_id)
    rows = kb.pack_long_list(
        [
            InlineKeyboardButton(
                text=f"{'✅' if prefs.get(key) else '⬜️'} {title}", callback_data=f"res:st:ntog:{key}"
            )
            for key, title, _, _default in catalog
        ]
    )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="— اعلانی برای دسترسی شما نیست —", callback_data="res:st:hub")]
        )
    rows.extend(
        [InlineKeyboardButton(text=title, callback_data=f"res:st:sub:notify:{sub_id}")]
        for sub_id, title, _fields in SECTIONS["notify"]["subs"]
    )
    rows.append([kb.back_button(None, "res:st:hub")])
    if callback.message:
        await safe_edit_text(
            callback.message,
            "🔔 <b>نوتیفیکیشن فروشگاه</b>\n"
            "مستقل از ادمین اصلی — فقط رویدادهای مجاز با دسترسی شما:",
            reply_markup=_kb(rows),
        )


async def _render_sub(
    callback: CallbackQuery,
    session: AsyncSession,
    sec_id: str,
    sub_id: str,
    reseller_id: int,
):
    found = resolve_sub(SECTIONS, sec_id, sub_id)
    fields = found[1][2] if found else None
    if found:
        sec_id, sub_id = found[0], found[1][0]
    if fields == "menu_layout":
        with _Scoped(reseller_id):
            cur = await get_setting(session, "menu_layout", reseller_id=reseller_id) or "compact"
            label = "فشرده (جفتی)" if cur == "compact" else "کلاسیک (تکی)"
            rows = [
                [
                    InlineKeyboardButton(
                        text=f"حالت: {label}",
                        callback_data="res:st:menu:layout",
                    )
                ],
                [kb.back_button(None, f"res:st:sec:{sec_id}")],
            ]
            if callback.message:
                await safe_edit_text(
                    callback.message,
                    "<b>چیدمان منو</b>\nروی دکمه بزنید تا بین فشرده (جفتی) و کلاسیک (تکی) عوض شود.",
                    reply_markup=_kb(rows),
                )
        return
    if fields == "menu_order":
        await _render_menu_order(callback, session, reseller_id, back=f"res:st:sec:{sec_id}")
        return
    panel = await _sub_panel(session, sec_id, sub_id, reseller_id)
    if panel is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if callback.message:
        await safe_edit_text(callback.message, panel[0], reply_markup=panel[1])


async def _sub_panel(session: AsyncSession, sec_id: str, sub_id: str, reseller_id: int) -> Panel | None:
    """A subsection's field list; None when it is not a plain field list."""
    found = resolve_sub(SECTIONS, sec_id, sub_id)
    if found is None or not isinstance(found[1][2], list):
        return None
    sec_id, (sub_id, title, fields) = found
    with _Scoped(reseller_id):
        rows = []
        for key, label, kind in fields:
            cur = await get_setting(session, key, reseller_id=reseller_id)
            if kind == PLAN_SCOPE_KIND:
                rows.append([plan_scope_button("res:st", key, label, cur)])
            elif kind == "toggle":
                mark = "✅" if on(cur) else "❌"
                rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"res:st:tog:{key}")])
            elif kind == "select":
                rows.append(
                    [InlineKeyboardButton(text=f"{label}: {select_label(key, cur)}", callback_data=f"res:st:sel:{key}")]
                )
            else:
                shown = secret_state(cur) if is_secret(kind) else _preview(cur, limit=28)
                rows.append(
                    [InlineKeyboardButton(text=f"✏️ {label}: {shown}", callback_data=f"res:st:edit:{key}")]
                )
    rows.extend(destination_list_rows(sec_id, sub_id, for_reseller=True))
    rows.append([kb.back_button(None, sub_back("res", sec_id, sub_id))])
    if sub_id == "bbc":
        from app.services.payment_providers.bezanbecard import SETUP_HELP_HTML

        return f"⚙️ <b>{title}</b>\n\n{SETUP_HELP_HTML}", _kb(rows)
    return f"⚙️ <b>{title}</b>", _kb(rows)


async def _location_panel(session: AsyncSession, profile, loc: list | tuple | None) -> Panel:
    """Where a settings prompt was opened: a subsection, a section, or the hub."""
    rid = profile.user_id
    panel = None
    if loc and len(loc) == 2:
        panel = await _sub_panel(session, loc[0], loc[1], rid) if loc[1] else await _section_panel(session, loc[0], rid)
    if panel is None:
        text, markup = await _hub_panel(session, profile)
        panel = (text, markup or _kb([]))
    return panel


async def _close_input_to_location(
    message: Message, state: FSMContext, session: AsyncSession, profile, *, note: str | None = None
) -> None:
    """Typed cancel or save: the prompt bubble becomes the screen it was opened from."""
    text, markup = await _location_panel(session, profile, (await state.get_data()).get("rs_loc"))
    await _close_input(message, state, text=f"{note}\n\n{text}" if note else text, markup=markup)


async def _typed_cancel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    profile,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    """A typed «انصراف» behaves exactly like the inline one: back to where the prompt opened."""
    from app.bot.nav_input import try_legacy_cancel

    if await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    await _close_input_to_location(message, state, session, profile)


async def reopen_shop_settings_location(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Inline «انصراف» (and typed, through ``try_legacy_cancel``) of a shop settings prompt."""
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await state.clear()
        await message.answer(err)
        return
    text, markup = await _location_panel(session, profile, (await state.get_data()).get("rs_loc"))
    await state.clear()
    # nav_inline imports this package's helpers; defer to avoid an import cycle.
    from app.bot.nav_inline import present_nav_panel

    await present_nav_panel(message, text=text, inline=markup, state=state)


def _owner_screen_for_key(key: str) -> tuple[str, str] | None:
    for sec_id, sec in SECTIONS.items():
        for sub_id, _label, payload in sec.get("subs") or []:
            if isinstance(payload, list):
                for f in payload:
                    if f[0] == key:
                        return sec_id, sub_id
    return None


@router.callback_query(F.data == "res:st:hub")
async def settings_hub(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _render_hub(
        callback,
        session,
        profile,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data.startswith("res:st:sec:"))
async def settings_section(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    sec_id = callback.data.split(":")[-1]
    await callback.answer()
    await _render_section(callback, session, sec_id, profile.user_id)


@router.callback_query(F.data.startswith("res:st:sub:"))
async def settings_sub(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    parts = callback.data.split(":")
    sec_id, sub_id = parts[3], parts[4]
    await callback.answer()
    await _render_sub(callback, session, sec_id, sub_id, profile.user_id)


@router.callback_query(F.data.startswith("res:st:lab:"))
async def settings_labels_audience(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    _profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    panel = labels_audience_panel("res", callback.data.rsplit(":", 1)[-1], SECTIONS[LABELS_SECTION]["subs"])
    if panel is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, panel[0], reply_markup=panel[1])


@router.callback_query(F.data.startswith("res:st:tog:"))
async def settings_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    if key.startswith("notify_") or not _is_field_toggle_key(key):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, key, reseller_id=profile.user_id)
        new_val = "0" if on(cur) else "1"
        # Same check as the web payment save: the gateway calls back over https only.
        refusal = (
            await bbc_enable_error(session, {key: new_val}, reseller_id=profile.user_id)
            if key == BBC_ENABLED_KEY
            else None
        )
        if refusal:
            await callback.answer(refusal, show_alert=True)
            return
        await set_setting(session, key, new_val, reseller_id=profile.user_id)
    await callback.answer(feedback.SAVED)
    loc = _owner_screen_for_key(key)
    if loc:
        await _render_sub(callback, session, loc[0], loc[1], profile.user_id)


async def _show_plan_picker(callback: CallbackQuery, session: AsyncSession, key: str, shop_id: int) -> None:
    loc = _owner_screen_for_key(key)
    back = f"res:st:sub:{loc[0]}:{loc[1]}" if loc else "res:st:hub"
    with _Scoped(shop_id):
        raw = await get_setting(session, key, reseller_id=shop_id)
    markup = plan_picker_markup("res:st", key, raw, await plan_choices(session, shop_id), back)
    if callback.message:
        await safe_edit_text(callback.message, plan_picker_text(FIELDS[key][1]), reply_markup=markup)


def _is_plan_scope_key(key: str) -> bool:
    meta = FIELDS.get(key)
    return bool(meta and meta[2] == PLAN_SCOPE_KIND)


@router.callback_query(F.data.startswith("res:st:plo:"))
async def settings_plan_scope_open(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    key = (callback.data or "").split(":", 3)[-1]
    if err or not _is_plan_scope_key(key):
        await callback.answer(err or feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    await _show_plan_picker(callback, session, key, profile.user_id)


@router.callback_query(F.data.regexp(r"^res:st:pls:[a-z_]{1,48}:(all|\d{1,10})$"))
async def settings_plan_scope_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    _, _, _, key, choice = (callback.data or "").split(":")
    if err or not _is_plan_scope_key(key):
        await callback.answer(err or feedback.INVALID, show_alert=True)
        return
    # Only this shop's own plans can be chosen; the scope is saved on this shop only.
    own = [c.plan_id for c in await plan_choices(session, profile.user_id)]
    with _Scoped(profile.user_id):
        try:
            value = toggle_plan_scope(await get_setting(session, key, reseller_id=profile.user_id), choice, own)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await set_setting(session, key, value, reseller_id=profile.user_id)
    await callback.answer(feedback.SAVED)
    await _show_plan_picker(callback, session, key, profile.user_id)


@router.callback_query(F.data.startswith("res:st:sel:"))
async def settings_select_cycle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    meta = FIELDS.get(key)
    if meta is None or meta[2] != "select" or key not in SELECT_OPTIONS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, key, reseller_id=profile.user_id)
        await set_setting(session, key, next_select_value(key, cur), reseller_id=profile.user_id)
    await callback.answer(feedback.SAVED)
    loc = _owner_screen_for_key(key)
    if loc:
        await _render_sub(callback, session, loc[0], loc[1], profile.user_id)


@router.callback_query(F.data == "res:st:menu:layout")
async def menu_layout_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    profile, err = await _gate(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if err:
        await callback.answer(err, show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, "menu_layout", reseller_id=profile.user_id) or "compact"
        await set_setting(
            session,
            "menu_layout",
            "compact" if cur == "classic" else "classic",
            reseller_id=profile.user_id,
        )
    await callback.answer(feedback.SAVED)
    await _render_sub(callback, session, "menu", "layout", profile.user_id)


@router.callback_query(F.data == "res:st:noop")
async def settings_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(
    F.data.startswith("res:st:menu:up:")
    | F.data.startswith("res:st:menu:dn:")
    | F.data.startswith("res:st:menu:add:")
    | F.data.startswith("res:st:menu:rm:")
)
async def menu_order_edit(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    profile, err = await _gate(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if err:
        await callback.answer(err, show_alert=True)
        return
    from app.bot.keyboards import DEFAULT_MENU_ORDER, sync_show_flags_for_order
    from app.services.users import set_settings_bulk

    parts = (callback.data or "").split(":")
    op = parts[3] if len(parts) > 3 else ""
    with _Scoped(profile.user_id):
        raw = await get_setting(session, "menu_order", reseller_id=profile.user_id) or ""
    order = [
        p.strip()
        for p in raw.split(",")
        if p.strip() and p.strip() in DEFAULT_MENU_ORDER and p.strip() not in {"reseller_apply", "miniapp"}
    ]
    if "shop" not in order:
        order.insert(0, "shop")
    if op in {"up", "dn"}:
        try:
            idx = int(parts[4])
        except (IndexError, ValueError):
            await callback.answer()
            return
        swap = idx - 1 if op == "up" else idx + 1
        if idx < 0 or idx >= len(order) or swap < 0 or swap >= len(order):
            await callback.answer("انتهای لیست")
            return
        order[idx], order[swap] = order[swap], order[idx]
    elif op == "add":
        key = parts[4] if len(parts) > 4 else ""
        if (
            key in DEFAULT_MENU_ORDER
            and key not in {"reseller_apply", "miniapp"}
            and key not in order
        ):
            order.append(key)
    elif op == "rm":
        key = parts[4] if len(parts) > 4 else ""
        if key and key != "shop" and key in order:
            order = [k for k in order if k != key]
    payload = {"menu_order": ",".join(order), **sync_show_flags_for_order(order)}
    payload["show_reseller_apply"] = "0"
    await set_settings_bulk(session, payload, reseller_id=profile.user_id)
    await callback.answer(feedback.SAVED)
    await _render_menu_order(
        callback, session, profile.user_id, back="res:st:sec:menu"
    )


@router.callback_query(F.data.startswith("res:st:ntog:"))
async def settings_notify_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Toggle shop notify prefs — ResellerSetting only, ACL + no platform-only keys."""
    profile, err = await _gate(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if err:
        await callback.answer(err, show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    from app.services.authz import resolve_shop_permissions_from_profile
    from app.services.notifications import (
        PLATFORM_ONLY_NOTIFY_KEYS,
        get_shop_notify_prefs,
        shop_notify_allowed_keys,
    )

    if key in PLATFORM_ONLY_NOTIFY_KEYS or not key.startswith("notify_"):
        await callback.answer("این اعلان برای فروشگاه مجاز نیست", show_alert=True)
        return
    perms = list(resolve_shop_permissions_from_profile(profile) or [])
    if key not in shop_notify_allowed_keys(perms):
        await callback.answer("دسترسی این اعلان را ندارید", show_alert=True)
        return
    prefs = await get_shop_notify_prefs(session, profile.user_id)
    new_val = "0" if prefs.get(key) else "1"
    await set_setting(session, key, new_val, reseller_id=profile.user_id)
    await callback.answer(feedback.SAVED)
    await _render_notify(callback, session, profile.user_id)


@router.callback_query(F.data.startswith("res:st:edit:"))
async def settings_edit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    meta = FIELDS.get(key)
    if not meta:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if meta[2] == "select":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, key, reseller_id=profile.user_id)
    if key == "force_join_channel":
        from app.services.users import format_force_join_for_edit

        cur = format_force_join_for_edit(cur)
    if cur and cur == NUMBER_UNLIMITED_VALUES.get(key):
        cur = UNLIMITED_LABEL
    await callback.answer()
    await state.update_data(edit_key=key, reseller_id=profile.user_id, rs_loc=_owner_screen_for_key(key))
    hint = feedback.ENTER_NEW_TEXT
    if meta[2] == "number":
        hint = number_range_hint(key)
    elif key == "force_join_channel":
        hint = "هر خط یک کانال (@channel یا آیدی).\nعضویت همه الزامی ذخیره می‌شود.\nبرای انصراف: انصراف"
    else:
        from app.services.rich_text import is_button_label_key, is_message_rich_key

        if is_button_label_key(key):
            hint += (
                "\n<i>ایموجی پریمیوم به‌عنوان آیکن دکمه "
                "(icon_custom_emoji_id) ذخیره می‌شود.</i>"
            )
        elif is_message_rich_key(key):
            hint += "\n<i>ایموجی پریمیوم در متن پیام حفظ می‌شود.</i>"
    template_hint = text_template_hint(key, actor="shop")
    if template_hint:
        hint += "\n" + template_hint
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=(
            secret_prompt(meta[1], cur)
            if is_secret(meta[2])
            else f"<b>{meta[1]}</b>\nفعلی:\n<code>{html.escape(_preview(cur))}</code>\n\n{hint}"
        ),
        cancel_code="rs_set",
        fsm_state=ResellerSettingsStates.edit_value,
        edit=True,
    )


async def _store_shop_setting(session: AsyncSession, profile, key: str, value: str) -> None:
    """Save one setting of the shop; its own bot's Telegram profile texts are pushed to Telegram first."""
    if key in PROFILE_TEXT_FIELDS:
        await update_bot_profile_text(
            session, token=reveal_bot_token(profile.bot_token), key=key, value=value, reseller_id=profile.user_id
        )
        return
    await set_setting(session, key, value, reseller_id=profile.user_id)


def _shop_typed_value(key: str, kind: str, text: str, message: Message) -> str:
    """The stored form of a typed shop setting; ``ValueError`` carries the message to show."""
    if is_secret(kind):
        return text
    if kind == "number":
        return parse_number_setting(key, text)
    if key in VALUE_NORMALIZERS:
        return VALUE_NORMALIZERS[key](text)
    return _shop_setting_value(key, message)


def _shop_setting_value(key: str, message: Message) -> str:
    """The stored form of a typed shop text: the same normalisation the owner bot and the web apply."""
    if key == "force_join_channel":
        return normalize_force_join_channel_value(message.text or "")
    if key == "receipt_dup_policy":
        return normalize_dup_policy(message.text or "")
    if wants_default_text(key, message.text):
        return ""
    return pack_setting_from_message(key, message)


@router.message(ResellerSettingsStates.edit_value)
async def settings_edit_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await state.clear()
        await message.answer(err)
        return
    data = await state.get_data()
    key = data.get("edit_key")
    text = (message.text or "").strip()
    if kb.is_cancel_text(text) or not key:
        await _typed_cancel(message, state, session, db_user, profile,
                            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
        return
    meta = FIELDS.get(key)
    if meta is None:
        await state.clear()
        await message.answer(feedback.INVALID)
        return
    if is_secret(meta[2]):
        await delete_user_message(message)
    try:
        await _store_shop_setting(session, profile, key, _shop_typed_value(key, meta[2], text, message))
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await _close_input_to_location(message, state, session, profile, note=feedback.SAVED_WITH_ICON)


@router.callback_query(F.data == "res:st:sup:add")
async def support_add(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.answer()
    await state.update_data(reseller_id=profile.user_id, rs_loc=("support", ""))
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="عنوان پشتیبان را بفرستید:",
        cancel_code="rs_set",
        fsm_state=ResellerSettingsStates.support_title,
        edit=True,
    )


@router.message(ResellerSettingsStates.support_title)
async def support_title_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await state.clear()
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await _typed_cancel(message, state, session, db_user, profile,
                            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
        return
    await state.update_data(support_title=text)
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt="یوزرنیم یا آیدی عددی تلگرام را بفرستید:",
        cancel_code="rs_set",
        fsm_state=ResellerSettingsStates.support_telegram,
        edit=False,
    )


@router.message(ResellerSettingsStates.support_telegram)
async def support_telegram_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await state.clear()
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await _typed_cancel(message, state, session, db_user, profile,
                            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
        return
    data = await state.get_data()
    title = data.get("support_title") or "پشتیبانی"
    _, err2 = await upsert_support_contact(
        session,
        contact_id=None,
        title=title,
        telegram=text,
        reseller_id=profile.user_id,
    )
    if err2:
        await state.clear()
        await message.answer(f"❌ {err2}")
        return
    await _close_input(
        message,
        state,
        text="✅ پشتیبان اضافه شد.",
        markup=_kb([[InlineKeyboardButton(text="💬 پشتیبان‌ها", callback_data="res:st:sec:support")]]),
    )


@router.callback_query(F.data.startswith("res:st:sup:del:"))
async def support_del(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    cid = callback.data.split(":")[-1]
    await delete_support_contact(session, cid, reseller_id=profile.user_id)
    await callback.answer(feedback.DELETED)
    await _render_section(callback, session, "support", profile.user_id)


@router.callback_query(F.data == "res:st:bot:token")
async def bot_token_ask(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.answer()
    await state.set_state(ResellerSettingsStates.bot_token)
    await state.update_data(reseller_id=profile.user_id, rs_loc=("bot", ""))
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="توکن جدید ربات را از @BotFather بفرستید:\nبرای انصراف: انصراف",
        cancel_code="rs_set",
        fsm_state=ResellerSettingsStates.bot_token,
        edit=True,
    )


@router.message(ResellerSettingsStates.bot_token)
async def bot_token_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await state.clear()
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await _typed_cancel(message, state, session, db_user, profile,
                            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
        return
    from app.services.resellers import complete_reseller_setup
    from app.services.reseller_bots import start_reseller_bot_for_profile

    try:
        from aiogram import Bot as TgBot

        bot = TgBot(token=text)
        try:
            me = await bot.get_me()
            uname = me.username or str(me.id)
            tg_id = int(me.id)
        finally:
            await bot.session.close()
        await complete_reseller_setup(
            session,
            profile,
            bot_token=text,
            bot_username=uname,
            bot_telegram_id=tg_id,
            bot_only=True,
        )
        await start_reseller_bot_for_profile(profile.id)
    except ValueError as e:
        await message.answer(f"❌ {domain_error_text(e)}")
        return
    except Exception:
        await message.answer("❌ توکن نامعتبر است")
        return
    await _close_input(
        message,
        state,
        text=f"✅ ربات @{uname} ثبت و راه‌اندازی شد.",
        markup=_kb([[InlineKeyboardButton(text="⚙️ تنظیمات", callback_data="res:st:hub")]]),
    )
