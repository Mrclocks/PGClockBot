"""Admin settings — Telegram-native nested menus (short screens, clear path)."""

from __future__ import annotations

import inspect
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import bot_admin_settings_in_flow, is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner
from app.db.models import BotUser, Plan
from app.services.notifications import NOTIFY_PREFS
from app.services.pasarguard import get_pg
from app.services.support_contacts import (
    delete_support_contact,
    get_support_contacts,
    support_chat_url,
    upsert_support_contact,
)
from app.services.users import get_all_settings, get_setting, on, set_setting

router = Router(name="admin_settings")


async def _ensure_settings_actor(
    *,
    session: AsyncSession | None,
    db_user: BotUser | None,
    callback: CallbackQuery | None = None,
    message: Message | None = None,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
) -> bool:
    """Owner gate for settings handlers.

    Inside owner-gated reply navigation (``NAV_ADMIN_SETTINGS``), skip the
    redundant Principal re-check that false-denied the real Owner. Outside that
    flow, enforce the same Owner gate as other admin routers.
    """
    event = callback or message
    if (
        event is not None
        and state is not None
        and await bot_admin_settings_in_flow(event, {"state": state})
    ):
        if not _is_admin(db_user):
            text = "ادمین نیستید"
            if callback is not None:
                await callback.answer(text, show_alert=True)
            elif message is not None:
                await message.answer(text)
            return False
        return True
    return await require_bot_owner(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        callback=callback,
        message=message,
    )


def settings_actor_required(fn):
    """Apply ``_ensure_settings_actor`` before settings router handlers."""
    sig = inspect.signature(fn)

    async def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        a = bound.arguments
        if not await _ensure_settings_actor(
            session=a.get("session"),
            db_user=a.get("db_user"),
            callback=a.get("callback"),
            message=a.get("message"),
            state=a.get("state"),
            is_reseller_bot=bool(a.get("is_reseller_bot", False)),
        ):
            return None
        call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return await fn(*args, **call_kwargs)

    wrapper.__signature__ = sig
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    wrapper.__qualname__ = getattr(fn, "__qualname__", wrapper.__name__)
    return wrapper

# ---------------------------------------------------------------------------
# Navigation tree: hub → section → (optional subsection) → fields
# Keep ≤6–8 buttons per screen. Short labels. No decorative/noop rows.
# ---------------------------------------------------------------------------

# field: (key, short_label, kind)  kind = toggle|text|textarea|number|select
Field = tuple[str, str, str]

SECTIONS: dict[str, dict] = {
    # Ops remote-control: short identity + essentials only.
    "shop": {
        "title": "فروشگاه",
        "subs": [
            ("identity", "نام و خوش‌آمد", [
                ("shop_title", "نام فروشگاه", "text"),
                ("welcome_text", "پیام /start", "textarea"),
            ]),
            ("texts", "متن‌های ضروری", [
                ("support_text", "متن پشتیبانی", "textarea"),
                ("referral_text", "متن دعوت", "textarea"),
                ("purchase_success_text", "موفقیت خرید", "textarea"),
                ("payment_reject_text", "رد پرداخت", "textarea"),
            ]),
            ("btn_labels", "متن دکمه‌های اصلی", [
                ("btn_shop", "خرید", "text"),
                ("btn_services", "سرویس‌ها", "text"),
                ("btn_wallet", "کیف پول", "text"),
                ("btn_support", "پشتیبانی", "text"),
                ("btn_referral", "دعوت", "text"),
                ("btn_wholesale", "فروش عمده", "text"),
                ("btn_menu_home", "منوی اصلی", "text"),
                ("btn_back", "بازگشت", "text"),
            ]),
        ],
    },
    "menu": {
        "title": "منو",
        "subs": [
            ("layout", "چیدمان کیبورد", "menu_layout"),
            ("order", "ترتیب دکمه‌ها", "menu_order"),
        ],
    },
    "pay": {
        "title": "پرداخت",
        "subs": [
            ("methods", "روش‌های فعال", [
                ("pay_wallet_enabled", "کیف پول", "toggle"),
                ("pay_card_enabled", "کارت به کارت", "toggle"),
                ("pay_gateway_enabled", "درگاه لینک", "toggle"),
                ("pay_psp_enabled", "درگاه API", "toggle"),
                ("pay_card_auto_enabled", "تأیید خودکار کارت", "toggle"),
                ("pay_crypto_enabled", "رمزارز", "toggle"),
                ("pay_stars_enabled", "استارز", "toggle"),
                ("pay_discount_enabled", "کد تخفیف", "toggle"),
                ("auto_approve_payments", "تأیید خودکار رسید", "toggle"),
            ]),
            ("card", "کارت به کارت", [
                ("card_number", "شماره کارت", "text"),
                ("card_holder", "صاحب کارت", "text"),
                ("card_pay_text", "راهنمای پرداخت", "textarea"),
            ]),
            ("gateway", "درگاه لینک", [
                ("gateway_name", "نام درگاه", "text"),
                ("gateway_link", "لینک", "text"),
                ("gateway_pay_text", "راهنما", "textarea"),
            ]),
            ("psp", "درگاه API", [
                ("psp_provider", "ارائه‌دهنده (zarinpal/mock)", "text"),
                ("psp_merchant_id", "مرچنت", "text"),
                ("psp_sandbox", "سندباکس", "toggle"),
                ("psp_pay_text", "راهنما", "textarea"),
                ("btn_pay_psp", "متن دکمه", "text"),
            ]),
            ("card_auto", "تأیید خودکار کارت", [
                ("card_auto_provider", "ارائه‌دهنده", "text"),
                ("card_auto_webhook_secret", "رمز وب‌هوک", "text"),
                ("card_auto_hint_text", "راهنما", "textarea"),
            ]),
            ("crypto", "رمزارز", [
                ("crypto_asset", "رمزارز", "text"),
                ("crypto_network", "شبکه", "text"),
                ("crypto_address", "آدرس ولت", "text"),
                ("crypto_pay_text", "راهنما", "textarea"),
            ]),
            ("stars", "استارز", [
                ("stars_toman_per_star", "تومان هر استارز", "number"),
                ("stars_title", "عنوان فاکتور", "text"),
                ("stars_description", "توضیح فاکتور", "text"),
            ]),
            ("pay_extra", "پاداش دعوت", [
                ("referral_bonus", "پاداش دعوت", "number"),
            ]),
        ],
    },
    "support": {
        "title": "پشتیبان‌ها",
        "kind": "supports",
    },
    # Day-to-day access controls (hub-facing).
    "access": {
        "title": "دسترسی",
        "subs": [
            ("qr", "QR اشتراک", [
                ("qr_enabled", "ارسال خودکار QR", "toggle"),
                ("show_sub_link_in_text", "لینک در کپشن", "toggle"),
                ("qr_caption", "کپشن QR", "textarea"),
            ]),
            ("force", "کانال اجباری", [
                ("force_join_enabled", "فعال", "toggle"),
                ("force_join_channel", "کانال‌ها (هر خط یکی)", "text"),
                ("force_join_msg", "متن پیام عضویت", "textarea"),
                ("btn_force_join", "متن دکمه لینک کانال", "text"),
                ("btn_force_join_check", "متن دکمه بررسی", "text"),
            ]),
            ("terms", "قوانین", [
                ("terms_entry_enabled", "فعال ورود", "toggle"),
                ("terms_entry_text", "متن قوانین ورود", "textarea"),
                ("terms_entry_btn", "دکمه موافقت ورود", "text"),
                ("terms_entry_reaccept", "پذیرش مجدد ورود", "toggle"),
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
    # Deep plan/naming tools — kept for callback compatibility (plans hub),
    # not listed on the settings reply keyboard (HUB_ORDER).
    "service": {
        "title": "پلن تست و نام‌گذاری",
        "subs": [
            ("naming", "نام در پاسارگارد", [
                ("pg_username_prefix", "پیشوند", "text"),
                ("pg_username_suffix", "پسوند", "text"),
                ("pg_username_pattern", "الگو", "text"),
            ]),
            ("trial", "پلن تست", "trial"),
            ("custom", "پلن دلخواه", "custom"),
        ],
    },
    "notify": {
        "title": "اعلان‌ها",
        "kind": "notify",
    },
}

# Reply-keyboard hub only — deep «service» stays reachable via plans callbacks.
HUB_ORDER = ["shop", "menu", "pay", "support", "access", "notify"]


CUSTOM_PRICE: list[Field] = [
    ("custom_plan_price_per_gb", "قیمت هر گیگ", "number"),
    ("custom_plan_price_per_day", "قیمت هر روز", "number"),
    ("custom_plan_min_gb", "حداقل گیگ", "number"),
    ("custom_plan_max_gb", "حداکثر گیگ", "number"),
    ("custom_plan_min_days", "حداقل روز", "number"),
    ("custom_plan_max_days", "حداکثر روز", "number"),
]

MENU_ORDER_LABELS = {
    "shop": "خرید",
    "services": "سرویس‌ها",
    "wallet": "کیف پول",
    "support": "پشتیبانی",
    "referral": "دعوت",
    "reseller_apply": "نمایندگی",
    "miniapp": "مینی‌اپ",
}


class SettingsStates(StatesGroup):
    edit_value = State()
    support_title = State()
    support_telegram = State()
    trial_name = State()
    trial_days = State()
    trial_gb = State()


def _field_lookup() -> dict[str, Field]:
    out: dict[str, Field] = {}
    for sec in SECTIONS.values():
        for sub in sec.get("subs") or []:
            if isinstance(sub[2], list):
                for f in sub[2]:
                    out[f[0]] = f
    for f in CUSTOM_PRICE:
        out[f[0]] = f
    return out


FIELDS = _field_lookup()

NOTIFY_KEYS: frozenset[str] = frozenset(key for key, *_ in NOTIFY_PREFS)

# Extra toggles rendered outside SECTIONS field lists
EXTRA_TOGGLE_KEYS: frozenset[str] = frozenset({"custom_plan_enabled", "trial_enabled"}) | NOTIFY_KEYS


def _is_toggleable_setting_key(key: str) -> bool:
    """True for keys the adm:st:tog handler may flip."""
    if key in EXTRA_TOGGLE_KEYS:
        return True
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


def _back_row(*buttons: tuple[str, str]) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t, callback_data=c) for t, c in buttons]


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ----- render helpers -----


_SETTINGS_HUB_TEXT = (
    "⚙️ <b>تنظیمات سریع</b>\n"
    "بخش‌ها را از کیبورد پایین انتخاب کنید.\n"
    "ظاهر، رنگ، گزارش روزانه و متن‌های بلند → وب‌پنل."
)


async def _panel_settings_url(
    session: AsyncSession | None = None,
    *,
    for_shop: bool = False,
) -> str:
    """Public panel URL for deep settings — never embeds tokens or secrets.

    Fail-soft: if env/settings are incomplete (tests, misconfig), return a
    relative path so hub chrome still renders without a Telegram URL button.
    """
    if for_shop:
        from app.services.resellers import get_reseller_panel_base_url

        if session is None:
            return "/shop-settings"
        try:
            base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
        except Exception:
            return "/shop-settings"
        return f"{base}/shop-settings" if base else "/shop-settings"
    try:
        from app.services.setup_wizard import default_panel_base_url

        base = (default_panel_base_url() or "").rstrip("/")
    except Exception:
        return "/settings"
    return f"{base}/settings" if base else "/settings"


async def _panel_link_row(
    session: AsyncSession | None = None,
    *,
    for_shop: bool = False,
) -> list[InlineKeyboardButton]:
    url = await _panel_settings_url(session, for_shop=for_shop)
    if not url.startswith("http"):
        return []
    return [
        InlineKeyboardButton(text="🌐 تنظیمات کامل در وب‌پنل", url=url),
    ]


async def _render_hub(callback: CallbackQuery, *, refresh_keyboard: bool = False) -> None:
    """Back to the settings hub. Section list lives on the reply keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    panel_row = await _panel_link_row(for_shop=False)
    if panel_row:
        rows.append(panel_row)
    markup = _kb(rows) if rows else None
    if callback.message:
        await callback.message.edit_text(
            _SETTINGS_HUB_TEXT,
            reply_markup=markup,
        )
        if not refresh_keyboard:
            return
        try:
            await callback.message.answer(
                "کیبورد تنظیمات:",
                reply_markup=kb.admin_settings_reply_keyboard(),
            )
        except Exception:
            pass


async def _render_section(callback: CallbackQuery, session: AsyncSession, sec_id: str) -> None:
    sec = SECTIONS.get(sec_id)
    if not sec:
        await _render_hub(callback)
        return

    if sec.get("kind") == "supports":
        await _render_supports(callback, session)
        return
    if sec.get("kind") == "notify":
        await _render_notify(callback, session)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for sub in sec.get("subs") or []:
        sub_id, title, _payload = sub[0], sub[1], sub[2]
        rows.append(
            [InlineKeyboardButton(text=title, callback_data=f"adm:st:sub:{sec_id}:{sub_id}")]
        )
    rows.append(_back_row(("⬅️ بازگشت", "adm:st:hub")))
    if callback.message:
        await callback.message.edit_text(
            f"⚙️ <b>{sec['title']}</b>\nزیر‌بخش را انتخاب کنید:",
            reply_markup=_kb(rows),
        )


def _field_button(ui: dict, key: str, label: str, kind: str) -> InlineKeyboardButton:
    if kind == "toggle":
        mark = "✅" if on(ui.get(key)) else "⬜️"
        return InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"adm:st:tog:{key}")
    return InlineKeyboardButton(text=label, callback_data=f"adm:st:edit:{key}")


async def _render_fields(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    title: str,
    fields: list[Field],
    back_cb: str,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    ui = await get_all_settings(session)
    rows = [[_field_button(ui, k, lab, kind)] for k, lab, kind in fields]
    if extra_rows:
        rows.extend(extra_rows)
    rows.append(_back_row(("⬅️ بازگشت", back_cb)))
    if callback.message:
        await callback.message.edit_text(
            f"<b>{title}</b>\nبرای تغییر، روی مورد بزنید.",
            reply_markup=_kb(rows),
        )


async def _render_sub(callback: CallbackQuery, session: AsyncSession, sec_id: str, sub_id: str) -> None:
    sec = SECTIONS.get(sec_id) or {}
    sub = next((s for s in (sec.get("subs") or []) if s[0] == sub_id), None)
    if not sub:
        await _render_section(callback, session, sec_id)
        return
    title = sub[1]
    payload = sub[2]
    back = f"adm:st:sec:{sec_id}"

    if payload == "menu_layout":
        ui = await get_all_settings(session)
        layout = ui.get("menu_layout") or "compact"
        label = "فشرده (جفتی)" if layout == "compact" else "کلاسیک (تکی)"
        rows = [
            [InlineKeyboardButton(text=f"حالت: {label}", callback_data="adm:st:menu:layout")],
            _back_row(("⬅️ بازگشت", back)),
        ]
        if callback.message:
            await callback.message.edit_text(
                "<b>چیدمان منو</b>\nروی دکمه بزنید تا عوض شود.",
                reply_markup=_kb(rows),
            )
        return
    if payload == "menu_order":
        await _render_menu_order(callback, session, back)
        return
    if payload == "trial":
        await _render_trial(callback, session)
        return
    if payload == "custom":
        ui = await get_all_settings(session)
        extra = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if on(ui.get('custom_plan_enabled')) else '⬜️'} فعال در فروشگاه",
                    callback_data="adm:st:tog:custom_plan_enabled",
                )
            ],
            [InlineKeyboardButton(text="اتصال پاسارگارد", callback_data="adm:custom")],
        ]
        await _render_fields(
            callback,
            session,
            title=title,
            fields=CUSTOM_PRICE,
            back_cb=back,
            extra_rows=extra,
        )
        return
    if isinstance(payload, list):
        await _render_fields(callback, session, title=title, fields=payload, back_cb=back)
        return
    await _render_section(callback, session, sec_id)


async def _render_menu_order(callback: CallbackQuery, session: AsyncSession, back: str) -> None:
    from app.bot.keyboards import DEFAULT_MENU_ORDER, REMOVED_MENU_KEYS

    ui = await get_all_settings(session)
    order = [
        p.strip()
        for p in (ui.get("menu_order") or "").split(",")
        if p.strip() and p.strip() in DEFAULT_MENU_ORDER and p.strip() not in REMOVED_MENU_KEYS
    ]
    if "shop" not in order:
        order.insert(0, "shop")
    rows: list[list[InlineKeyboardButton]] = []
    for i, key in enumerate(order[:10]):
        label = MENU_ORDER_LABELS.get(key, key)
        row = [InlineKeyboardButton(text=f"{i + 1}. {label}", callback_data="adm:st:noop")]
        if i > 0:
            row.append(InlineKeyboardButton(text="⬆️", callback_data=f"adm:st:menu:up:{i}"))
        if i < len(order) - 1:
            row.append(InlineKeyboardButton(text="⬇️", callback_data=f"adm:st:menu:dn:{i}"))
        rows.append(row)
    rows.append(_back_row(("⬅️ بازگشت", back)))
    if callback.message:
        await callback.message.edit_text(
            "<b>منوی فعال</b>\nبا فلش ترتیب را عوض کنید.\n"
            "برای افزودن/حذف آیتم‌ها از وب‌پنل → تنظیمات → منوی بات استفاده کنید.",
            reply_markup=_kb(rows),
        )


async def _render_supports(callback: CallbackQuery, session: AsyncSession) -> None:
    contacts = await get_support_contacts(session)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ پشتیبان جدید", callback_data="adm:st:sup:add")]
    ]
    for c in contacts[:12]:
        mark = "✅" if c.get("enabled", True) else "⏸"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {c['title']}"[:40],
                    callback_data=f"adm:st:sup:v:{c['id']}",
                )
            ]
        )
    if not contacts:
        rows.append([InlineKeyboardButton(text="لیست خالی است", callback_data="adm:st:noop")])
    rows.append(_back_row(("⬅️ بازگشت", "adm:st:hub")))
    if callback.message:
        await callback.message.edit_text(
            "🎧 <b>پشتیبان‌ها</b>\n"
            "۱ نفر → دکمه پشتیبانی مستقیم به چت می‌رود\n"
            "چند نفر → لیست انتخاب برای کاربر",
            reply_markup=_kb(rows),
        )


async def _render_notify(callback: CallbackQuery, session: AsyncSession) -> None:
    ui = await get_all_settings(session)
    rows: list[list[InlineKeyboardButton]] = []
    for key, title, _, default in NOTIFY_PREFS:
        mark = "✅" if on(ui.get(key, default)) else "⬜️"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"adm:st:tog:{key}")]
        )
    rows.append(_back_row(("⬅️ بازگشت", "adm:st:hub")))
    if callback.message:
        await callback.message.edit_text(
            "🔔 <b>اعلان‌های ادمین اصلی</b>\n"
            "روشن/خاموش کنید (مستقل از نمایندگان):",
            reply_markup=_kb(rows),
        )


async def _render_trial(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(select(Plan).where(Plan.is_trial.is_(True)))
    trial = result.scalar_one_or_none()
    ui = await get_all_settings(session)
    if trial:
        gb = f"{trial.data_limit_gb:g} گیگ" if trial.data_limit_gb is not None else "نامحدود"
        if trial.pg_template_id:
            link = f"تمپلیت #{trial.pg_template_id}"
        elif trial.pg_group_ids:
            link = f"گروه {trial.pg_group_ids}"
        else:
            link = "بدون اتصال"
        body = (
            f"نام: <b>{trial.name}</b>\n"
            f"مدت: {trial.duration_days} روز · حجم: {gb}\n"
            f"اتصال: {link}"
        )
    else:
        body = "هنوز ساخته نشده."
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if on(ui.get('trial_enabled')) else '⬜️'} نمایش در فروشگاه",
                callback_data="adm:st:tog:trial_enabled",
            )
        ],
        [InlineKeyboardButton(text="نام", callback_data="adm:st:trial:name")],
        [InlineKeyboardButton(text="مدت (روز)", callback_data="adm:st:trial:days")],
        [InlineKeyboardButton(text="حجم (گیگ)", callback_data="adm:st:trial:gb")],
        [InlineKeyboardButton(text="تمپلیت پاسارگارد", callback_data="adm:st:trial:tpl")],
        [InlineKeyboardButton(text="گروه پاسارگارد", callback_data="adm:st:trial:grp")],
        _back_row(("⬅️ پلن‌ها", "adm:plans:kind:users:trial")),
    ]
    if callback.message:
        await callback.message.edit_text(
            f"🧪 <b>پلن تست</b>\n\n{body}",
            reply_markup=_kb(rows),
        )


def _owner_screen_for_key(key: str) -> tuple[str, str] | None:
    """Return (sec_id, sub_id) that owns this key for re-render after toggle/edit."""
    if key.startswith("notify_"):
        return ("notify", "")
    if key == "menu_layout":
        return ("menu", "layout")
    if key == "trial_enabled":
        return ("service", "trial")
    if key.startswith("custom_plan_"):
        return ("service", "custom")
    # Prefer exact field ownership (e.g. show_sub_link_in_text under access/qr)
    for sec_id, sec in SECTIONS.items():
        for sub in sec.get("subs") or []:
            payload = sub[2]
            if isinstance(payload, list) and any(f[0] == key for f in payload):
                return (sec_id, sub[0])
    # Legacy menu visibility flags (show_shop, …) — reorder screen
    if key.startswith("show_"):
        return ("menu", "order")
    return None


async def _rerender_after_key(callback: CallbackQuery, session: AsyncSession, key: str) -> None:
    loc = _owner_screen_for_key(key)
    if not loc:
        await _render_hub(callback)
        return
    sec_id, sub_id = loc
    if sec_id == "notify":
        await _render_notify(callback, session)
    elif not sub_id:
        await _render_section(callback, session, sec_id)
    else:
        await _render_sub(callback, session, sec_id, sub_id)


async def _keep_settings_nav(state: FSMContext | None) -> None:
    """After FSM clear, stay inside Settings so adm:st:* callbacks keep working."""
    if state is None:
        return
    from app.bot import menu_nav as nav

    await nav.set_nav_level(state, nav.NAV_ADMIN_SETTINGS, push=False)


async def _answer_fields(
    message: Message,
    session: AsyncSession,
    *,
    title: str,
    fields: list[Field],
    back_cb: str,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    ui = await get_all_settings(session)
    rows = [[_field_button(ui, k, lab, kind)] for k, lab, kind in fields]
    if extra_rows:
        rows.extend(extra_rows)
    rows.append(_back_row(("⬅️ بازگشت", back_cb)))
    await message.answer(
        f"<b>{title}</b>\nبرای تغییر، روی مورد بزنید.",
        reply_markup=_kb(rows),
    )


async def _answer_settings_location(
    message: Message,
    session: AsyncSession,
    loc: tuple[str, str] | None,
) -> None:
    """Re-open the subsection the operator was editing (new message, one Back)."""
    if not loc:
        return
    sec_id, sub_id = loc
    if sec_id == "notify":
        ui = await get_all_settings(session)
        rows: list[list[InlineKeyboardButton]] = []
        for key, title, _, default in NOTIFY_PREFS:
            mark = "✅" if on(ui.get(key, default)) else "⬜️"
            rows.append(
                [InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"adm:st:tog:{key}")]
            )
        rows.append(_back_row(("⬅️ بازگشت", "adm:st:hub")))
        await message.answer(
            "🔔 <b>اعلان‌های ادمین اصلی</b>\n"
            "روشن/خاموش کنید (مستقل از نمایندگان):",
            reply_markup=_kb(rows),
        )
        return
    if not sub_id:
        sec = SECTIONS.get(sec_id)
        if not sec:
            return
        rows: list[list[InlineKeyboardButton]] = []
        for sub in sec.get("subs") or []:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=sub[1],
                        callback_data=f"adm:st:sub:{sec_id}:{sub[0]}",
                    )
                ]
            )
        rows.append(_back_row(("⬅️ بازگشت", "adm:st:hub")))
        await message.answer(
            f"⚙️ <b>{sec['title']}</b>\nزیر‌بخش را انتخاب کنید:",
            reply_markup=_kb(rows),
        )
        return

    sec = SECTIONS.get(sec_id) or {}
    sub = next((s for s in (sec.get("subs") or []) if s[0] == sub_id), None)
    if not sub:
        return
    title = sub[1]
    payload = sub[2]
    back = f"adm:st:sec:{sec_id}"
    if payload == "trial":
        await message.answer(
            "🧪 <b>پلن تست</b>\nاز دکمه زیر ادامه دهید.",
            reply_markup=_kb(
                [
                    [
                        InlineKeyboardButton(
                            text="⬅️ بازگشت به پلن تست",
                            callback_data="adm:st:sub:service:trial",
                        )
                    ]
                ]
            ),
        )
        return
    if payload == "custom":
        ui = await get_all_settings(session)
        extra = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if on(ui.get('custom_plan_enabled')) else '⬜️'} فعال در فروشگاه",
                    callback_data="adm:st:tog:custom_plan_enabled",
                )
            ],
            [InlineKeyboardButton(text="اتصال پاسارگارد", callback_data="adm:custom")],
        ]
        await _answer_fields(
            message,
            session,
            title=title,
            fields=CUSTOM_PRICE,
            back_cb=back,
            extra_rows=extra,
        )
        return
    if isinstance(payload, list):
        await _answer_fields(message, session, title=title, fields=payload, back_cb=back)
        return
    # menu_layout / menu_order — one Back into that sub (callback still works with nav kept)
    await message.answer(
        f"<b>{title}</b>",
        reply_markup=_kb(
            [
                [
                    InlineKeyboardButton(
                        text="⬅️ بازگشت",
                        callback_data=f"adm:st:sub:{sec_id}:{sub_id}",
                    )
                ]
            ]
        ),
    )


async def _finish_settings_text_edit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    loc: tuple[str, str] | None,
    note: str = "✅ ذخیره شد.",
) -> None:
    """Confirm save, restore settings reply KB, return to previous subsection.

    No dual «بازگشت» rows — hub chrome is never forced here.
    """
    await state.clear()
    await _keep_settings_nav(state)
    await message.answer(note, reply_markup=kb.admin_settings_reply_keyboard())
    try:
        await _answer_settings_location(message, session, loc)
    except Exception:
        pass



# ----- callbacks: navigation -----


@router.callback_query(F.data == "adm:st:noop")
async def settings_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.in_({"adm:settings", "adm:st:hub"}))
async def settings_hub(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
):
    """Inline «⬅️ بازگشت» from a settings subsection.

    This is navigation (pop back to the hub), not an Owner gate. The operator
    is already inside Settings; showing «دسترسی مالک سیستم لازم است» here
    traps them. Hub text is not sensitive; the reply keyboard is already shown.
    """
    _ = (session, db_user, is_reseller_bot)
    await callback.answer()
    await _render_hub(callback, refresh_keyboard=False)


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sec:"))
async def settings_section(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    sec_id = callback.data.split(":")[-1]
    await callback.answer()
    await _render_section(callback, session, sec_id)


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sub:"))
async def settings_sub(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    # adm:st:sub:{sec}:{sub}
    parts = callback.data.split(":")
    sec_id, sub_id = parts[3], parts[4]
    await callback.answer()
    await _render_sub(callback, session, sec_id, sub_id)


# ----- toggle / edit -----


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:tog:"))
async def settings_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    if not _is_toggleable_setting_key(key):
        await callback.answer("کلید نامعتبر", show_alert=True)
        return
    cur = await get_setting(session, key)
    new_val = "0" if on(cur) else "1"
    await set_setting(session, key, new_val)
    if key == "trial_enabled":
        trial = (await session.execute(select(Plan).where(Plan.is_trial.is_(True)))).scalar_one_or_none()
        if trial:
            trial.is_active = new_val == "1"
            await session.commit()
    await callback.answer("ذخیره شد")
    await _rerender_after_key(callback, session, key)


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:edit:"))
async def settings_edit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    key = callback.data.split(":", 3)[-1]
    if key not in FIELDS:
        await callback.answer("کلید نامعتبر", show_alert=True)
        return
    meta = FIELDS.get(key)
    label = meta[1] if meta else key
    kind = meta[2] if meta else "text"
    cur = await get_setting(session, key)
    if key == "force_join_channel":
        from app.services.users import format_force_join_for_edit

        cur = format_force_join_for_edit(cur)
    loc = _owner_screen_for_key(key)
    await callback.answer()
    await state.set_state(SettingsStates.edit_value)
    await state.update_data(edit_key=key, edit_loc=loc)
    hint = "عدد بفرستید." if kind == "number" else "متن جدید را بفرستید.\nبرای انصراف: انصراف"
    if key == "force_join_channel":
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
    if callback.message:
        await callback.message.answer(
            f"<b>{label}</b>\nفعلی:\n<code>{_preview(cur)}</code>\n\n{hint}",
            reply_markup=kb.cancel_reply(),
        )


@settings_actor_required
@router.message(SettingsStates.edit_value)
async def settings_edit_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    data = await state.get_data()
    key = data.get("edit_key")
    loc = data.get("edit_loc")
    text = (message.text or "").strip()
    if kb.is_cancel_text(text) or not key:
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    meta = FIELDS.get(key)
    kind = meta[2] if meta else "text"
    if kind == "number":
        raw = text.replace(",", "").replace("٬", "")
        try:
            float(raw)
        except ValueError:
            await message.answer("عدد معتبر بفرستید")
            return
        text = raw
        await set_setting(session, key, text)
    elif key == "force_join_channel":
        from app.services.users import normalize_force_join_channel_value

        text = normalize_force_join_channel_value(text)
        await set_setting(session, key, text)
    else:
        from app.services.rich_text import pack_setting_from_message

        text = pack_setting_from_message(key, message)
        await set_setting(session, key, text)
    await _finish_settings_text_edit(message, state, session, loc=loc)


@settings_actor_required
@router.callback_query(F.data == "adm:st:menu:layout")
async def menu_layout_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cur = await get_setting(session, "menu_layout") or "compact"
    await set_setting(session, "menu_layout", "compact" if cur == "classic" else "classic")
    await callback.answer("ذخیره شد")
    await _render_sub(callback, session, "menu", "layout")


@settings_actor_required
@router.callback_query(
    F.data.startswith("adm:st:menu:up:") | F.data.startswith("adm:st:menu:dn:")
)
async def menu_reorder(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    direction, idx = parts[3], int(parts[4])
    from app.bot.keyboards import DEFAULT_MENU_ORDER, REMOVED_MENU_KEYS, sync_show_flags_for_order

    order = [
        p.strip()
        for p in (await get_setting(session, "menu_order") or "").split(",")
        if p.strip() and p.strip() in DEFAULT_MENU_ORDER and p.strip() not in REMOVED_MENU_KEYS
    ]
    if "shop" not in order:
        order.insert(0, "shop")
    if idx < 0 or idx >= len(order):
        await callback.answer()
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if swap < 0 or swap >= len(order):
        await callback.answer("انتهای لیست")
        return
    order[idx], order[swap] = order[swap], order[idx]
    await set_setting(session, "menu_order", ",".join(order))

    for key, val in sync_show_flags_for_order(order).items():
        await set_setting(session, key, val)
    await callback.answer()
    await _render_menu_order(callback, session, "adm:st:sec:menu")


# ----- Supports -----


@settings_actor_required
@router.callback_query(F.data == "adm:st:sup:add")
async def support_add_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
):
    _ = session
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SettingsStates.support_title)
    await state.update_data(support_edit_id=None)
    if callback.message:
        await callback.message.answer(
            "عنوان پشتیبان (مثلاً پشتیبان ربات):",
            reply_markup=kb.cancel_reply(),
        )


@settings_actor_required
@router.message(SettingsStates.support_title)
async def support_title_msg(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
):
    _ = session
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    await state.update_data(support_title=text)
    await state.set_state(SettingsStates.support_telegram)
    await message.answer(
        "یوزرنیم یا آیدی تلگرام را بفرستید:\n<code>@user</code> یا عدد",
        reply_markup=kb.cancel_reply(),
    )


@settings_actor_required
@router.message(SettingsStates.support_telegram)
async def support_telegram_msg(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    data = await state.get_data()
    await state.clear()
    item, err = await upsert_support_contact(
        session,
        contact_id=data.get("support_edit_id"),
        title=data.get("support_title") or "پشتیبان",
        telegram=text,
        enabled=True,
    )
    if err:
        await message.answer(f"❌ {err}")
        return
    await message.answer(
        f"ذخیره شد ✅\n{item['title']} — <code>{item['telegram']}</code>",
        reply_markup=_kb([[InlineKeyboardButton(text="🎧 پشتیبان‌ها", callback_data="adm:st:sec:support")]]),
    )


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sup:v:"))
async def support_view(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cid = callback.data.split(":")[-1]
    c = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    url = support_chat_url(c["telegram"]) or "—"
    text = (
        f"🎧 <b>{c['title']}</b>\n"
        f"<code>{c['telegram']}</code>\n"
        f"{url}\n"
        f"{'فعال' if c.get('enabled', True) else 'خاموش'}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="خاموش" if c.get("enabled", True) else "روشن",
                callback_data=f"adm:st:sup:tog:{cid}",
            ),
            InlineKeyboardButton(text="🎨 رنگ", callback_data=f"adm:st:sup:color:{cid}"),
            InlineKeyboardButton(text="ویرایش", callback_data=f"adm:st:sup:edit:{cid}"),
            InlineKeyboardButton(text="حذف", callback_data=f"adm:st:sup:del:{cid}"),
        ],
        _back_row(("⬅️ لیست", "adm:st:sec:support")),
    ]
    if callback.message:
        await callback.message.edit_text(text, reply_markup=_kb(rows))


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sup:tog:"))
async def support_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cid = callback.data.split(":")[-1]
    c = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await upsert_support_contact(
        session,
        contact_id=cid,
        title=c["title"],
        telegram=c["telegram"],
        sort=int(c.get("sort") or 0),
        enabled=not c.get("enabled", True),
    )
    await callback.answer("ذخیره شد")
    c2 = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c2 or not callback.message:
        return
    url = support_chat_url(c2["telegram"]) or "—"
    text = (
        f"🎧 <b>{c2['title']}</b>\n<code>{c2['telegram']}</code>\n{url}\n"
        f"{'فعال' if c2.get('enabled', True) else 'خاموش'}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="خاموش" if c2.get("enabled", True) else "روشن",
                callback_data=f"adm:st:sup:tog:{cid}",
            ),
            InlineKeyboardButton(text="🎨 رنگ", callback_data=f"adm:st:sup:color:{cid}"),
            InlineKeyboardButton(text="ویرایش", callback_data=f"adm:st:sup:edit:{cid}"),
            InlineKeyboardButton(text="حذف", callback_data=f"adm:st:sup:del:{cid}"),
        ],
        _back_row(("⬅️ لیست", "adm:st:sec:support")),
    ]
    await callback.message.edit_text(text, reply_markup=_kb(rows))


@settings_actor_required
@router.callback_query(F.data.regexp(r"^adm:st:sup:color:[^:]+$"))
async def support_color_picker(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cid = callback.data.split(":")[-1]
    c = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    ui = await get_all_settings(session)
    markup = kb.item_button_style_picker_keyboard(
        callback_prefix=f"adm:st:sup:color:set:{cid}",
        back_callback=f"adm:st:sup:v:{cid}",
        inherit_label="ارث از پشتیبانی",
        ui=ui,
    )
    if callback.message:
        await callback.message.edit_text(
            f"🎨 رنگ دکمه «{c['title']}» را انتخاب کنید:",
            reply_markup=markup,
        )


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sup:color:set:"))
async def support_color_set(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.button_styles import parse_item_button_style_callback

    parts = callback.data.split(":")
    if len(parts) < 7:
        await callback.answer("نامعتبر", show_alert=True)
        return
    cid = parts[5]
    style = parse_item_button_style_callback(parts[6])
    c = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await upsert_support_contact(
        session,
        contact_id=cid,
        title=c["title"],
        telegram=c["telegram"],
        sort=int(c.get("sort") or 0),
        enabled=bool(c.get("enabled", True)),
        button_style=style,
    )
    await callback.answer("ذخیره شد")
    c2 = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c2 or not callback.message:
        return
    url = support_chat_url(c2["telegram"]) or "—"
    text = (
        f"🎧 <b>{c2['title']}</b>\n"
        f"<code>{c2['telegram']}</code>\n"
        f"{url}\n"
        f"{'فعال' if c2.get('enabled', True) else 'خاموش'}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="خاموش" if c2.get("enabled", True) else "روشن",
                callback_data=f"adm:st:sup:tog:{cid}",
            ),
            InlineKeyboardButton(text="🎨 رنگ", callback_data=f"adm:st:sup:color:{cid}"),
            InlineKeyboardButton(text="ویرایش", callback_data=f"adm:st:sup:edit:{cid}"),
            InlineKeyboardButton(text="حذف", callback_data=f"adm:st:sup:del:{cid}"),
        ],
        _back_row(("⬅️ لیست", "adm:st:sec:support")),
    ]
    await callback.message.edit_text(text, reply_markup=_kb(rows))


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sup:edit:"))
async def support_edit_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cid = callback.data.split(":")[-1]
    c = next((x for x in await get_support_contacts(session) if x["id"] == cid), None)
    if not c:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SettingsStates.support_title)
    await state.update_data(support_edit_id=cid)
    if callback.message:
        await callback.message.answer(
            f"عنوان جدید (فعلی: {c['title']}):",
            reply_markup=kb.cancel_reply(),
        )


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:sup:del:"))
async def support_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await delete_support_contact(session, callback.data.split(":")[-1])
    await callback.answer("حذف شد")
    await _render_supports(callback, session)


# ----- Trial -----


async def _ensure_trial(session: AsyncSession) -> Plan:
    trial = (await session.execute(select(Plan).where(Plan.is_trial.is_(True)))).scalar_one_or_none()
    if trial:
        return trial
    trial = Plan(
        name="تست رایگان",
        price=0,
        duration_days=1,
        data_limit_gb=1,
        is_trial=True,
        is_active=True,
        description="پلن تست رایگان",
    )
    session.add(trial)
    await session.commit()
    await session.refresh(trial)
    return trial


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:name")
async def trial_ask_name(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SettingsStates.trial_name)
    if callback.message:
        await callback.message.answer("نام پلن تست:", reply_markup=kb.cancel_reply())


@settings_actor_required
@router.message(SettingsStates.trial_name)
async def trial_save_name(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    trial = await _ensure_trial(session)
    trial.name = text[:128]
    await session.commit()
    await _finish_settings_text_edit(
        message,
        state,
        session,
        loc=("service", "trial"),
    )


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:days")
async def trial_ask_days(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SettingsStates.trial_days)
    if callback.message:
        await callback.message.answer("مدت به روز:", reply_markup=kb.cancel_reply())


@settings_actor_required
@router.message(SettingsStates.trial_days)
async def trial_save_days(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    try:
        days = max(1, int(text))
    except ValueError:
        await message.answer("عدد معتبر")
        return
    trial = await _ensure_trial(session)
    trial.duration_days = days
    await session.commit()
    await _finish_settings_text_edit(
        message,
        state,
        session,
        loc=("service", "trial"),
    )


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:gb")
async def trial_ask_gb(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SettingsStates.trial_gb)
    if callback.message:
        await callback.message.answer("حجم به گیگ (۰ = نامحدود):", reply_markup=kb.cancel_reply())


@settings_actor_required
@router.message(SettingsStates.trial_gb)
async def trial_save_gb(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await _keep_settings_nav(state)
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_settings_reply_keyboard(),
        )
        return
    try:
        gb = float(text)
    except ValueError:
        await message.answer("عدد معتبر")
        return
    trial = await _ensure_trial(session)
    trial.data_limit_gb = None if gb <= 0 else gb
    await session.commit()
    await _finish_settings_text_edit(
        message,
        state,
        session,
        loc=("service", "trial"),
    )


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:tpl")
async def trial_pick_tpl(
    callback: CallbackQuery,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    try:
        templates = await get_pg().get_user_templates_simple()
    except Exception:
        templates = []
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:15]:
        tid = t.get("id")
        if tid is None:
            continue
        name = t.get("name") or f"تمپلیت {tid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{tid} {name}"[:40],
                    callback_data=f"adm:st:trial:settpl:{tid}",
                )
            ]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="تمپلیتی نیست", callback_data="adm:st:sub:service:trial")])
    rows.append(_back_row(("⬅️ بازگشت", "adm:st:sub:service:trial")))
    if callback.message:
        await callback.message.edit_text("تمپلیت را انتخاب کنید:", reply_markup=_kb(rows))


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:trial:settpl:"))
async def trial_set_tpl(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    tid = int(callback.data.split(":")[-1])
    trial = await _ensure_trial(session)
    trial.pg_template_id = tid
    trial.pg_group_ids = None
    await session.commit()
    await callback.answer("ذخیره شد")
    await _render_trial(callback, session)


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:grp")
async def trial_pick_grp(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    trial = await _ensure_trial(session)
    selected: list[int] = []
    if trial.pg_group_ids:
        for part in str(trial.pg_group_ids).split(","):
            if part.strip().isdigit():
                selected.append(int(part.strip()))
    await state.update_data(trial_groups=selected)
    await callback.answer()
    await _show_trial_groups(callback, state)


async def _show_trial_groups(callback: CallbackQuery, state: FSMContext) -> None:
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:20]:
        gid = g.get("id")
        if gid is None:
            continue
        gid = int(gid)
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{name}"[:40],
                    callback_data=f"adm:st:trial:toggrp:{gid}",
                )
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"تأیید ({len(selected)})",
                    callback_data="adm:st:trial:grpdone",
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="گروهی نیست", callback_data="adm:st:sub:service:trial")])
    rows.append(_back_row(("⬅️ بازگشت", "adm:st:sub:service:trial")))
    if callback.message:
        await callback.message.edit_text("گروه(ها) را انتخاب کنید:", reply_markup=_kb(rows))


@settings_actor_required
@router.callback_query(F.data.startswith("adm:st:trial:toggrp:"))
async def trial_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    gid = int(callback.data.split(":")[-1])
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(trial_groups=selected)
    await callback.answer()
    await _show_trial_groups(callback, state)


@settings_actor_required
@router.callback_query(F.data == "adm:st:trial:grpdone")
async def trial_grp_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه", show_alert=True)
        return
    trial = await _ensure_trial(session)
    trial.pg_group_ids = ",".join(str(x) for x in selected)
    trial.pg_template_id = None
    await session.commit()
    await state.update_data(trial_groups=[])
    await callback.answer("ذخیره شد")
    await _render_trial(callback, session)
