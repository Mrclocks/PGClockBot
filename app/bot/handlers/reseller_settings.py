"""Reseller shop settings in Telegram — subset of admin settings, scoped to their bot."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.resellers import get_reseller_profile, has_bot_perm
from app.services.support_contacts import (
    delete_support_contact,
    get_support_contacts,
    upsert_support_contact,
)
from app.services.users import (
    get_setting,
    on,
    reset_shop_reseller_id,
    set_setting,
    set_shop_reseller_id,
)
from app.services.settings_button_labels import (
    BTN_PAY_CARD,
    BTN_PAY_CRYPTO,
    BTN_PAY_GATEWAY,
    BTN_PAY_PSP,
    BTN_PAY_STARS,
    BTN_PAY_WALLET_DISCOUNT,
    shop_button_subs,
)


async def _lasting_kb(session, db_user, classic, *, is_reseller_bot=False, reseller_owner_id=None):
    from app.bot.nav_chrome import lasting_staff_reply
    return await lasting_staff_reply(
        session, db_user, classic=classic,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

router = Router(name="reseller_settings")

Field = tuple[str, str, str]

SECTIONS: dict[str, dict] = {
    # Same IA as owner hub — shop-scoped only (no platform keys).
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
            *shop_button_subs(include_platform=False),
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
                ("pay_card_auto_enabled", "تأیید خودکار کارت به کارت", "toggle"),
                ("pay_gateway_enabled", "درگاه لینک", "toggle"),
                ("pay_psp_enabled", "درگاه API", "toggle"),
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
                ("card_pay_text", "راهنمای پرداخت", "textarea"),
                *BTN_PAY_CARD,
            ]),
            ("card_auto", "تأیید خودکار کارت به کارت", [
                ("card_auto_provider", "ارائه‌دهنده", "text"),
                ("card_auto_webhook_secret", "رمز وب‌هوک", "text"),
                ("card_auto_hint_text", "راهنما", "textarea"),
            ]),
            ("gateway", "درگاه لینک", [
                ("gateway_name", "نام درگاه", "text"),
                ("gateway_link", "لینک", "text"),
                ("gateway_pay_text", "راهنما", "textarea"),
                *BTN_PAY_GATEWAY,
            ]),
            ("psp", "درگاه API", [
                ("psp_provider", "ارائه‌دهنده (zarinpal/mock)", "text"),
                ("psp_merchant_id", "مرچنت", "text"),
                ("psp_sandbox", "سندباکس", "toggle"),
                ("psp_pay_text", "راهنما", "textarea"),
                *BTN_PAY_PSP,
            ]),
            ("crypto", "رمزارز", [
                ("crypto_asset", "رمزارز", "text"),
                ("crypto_network", "شبکه", "text"),
                ("crypto_address", "آدرس ولت", "text"),
                ("crypto_pay_text", "راهنما", "textarea"),
                *BTN_PAY_CRYPTO,
            ]),
        ],
    },
    "support": {
        "title": "پشتیبان‌ها",
        "kind": "supports",
    },
    "access": {
        "title": "دسترسی",
        "subs": [
            ("qr", "QR اشتراک", [
                ("qr_enabled", "ارسال خودکار QR", "toggle"),
                ("show_sub_link_in_text", "لینک در کپشن", "toggle"),
                ("qr_caption", "کپشن QR", "textarea"),
            ]),
        ],
    },
    "limits": {
        "title": "محدودیت",
        "subs": [
            ("referral", "معرف اجباری", [
                ("referral_required", "معرف اجباری", "toggle"),
                ("referral_required_text", "پیام دریافت معرف", "textarea"),
            ]),
            ("trial_gate", "اکانت تست", [
                ("trial_require_contact", "تأیید شماره تماس", "toggle"),
                ("trial_require_iran_phone", "فقط شماره ایران", "toggle"),
            ]),
            ("purchase_contact", "شماره تماس خرید", [
                ("purchase_require_contact", "شماره تماس اجباری", "toggle"),
            ]),
            ("receipt_dup", "ضدتقلب رسید", [
                ("receipt_dup_policy", "رسید تکراری (warn/block)", "text"),
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
    "guides": {
        "title": "آموزش اتصال",
        "subs": [
            ("labels", "متن دکمه‌ها", [
                ("btn_guides", "دکمه فهرست آموزش", "text"),
                ("btn_guide_open_link", "دکمه لینک اشتراک", "text"),
            ]),
            ("hint", "ویرایش آموزش‌ها", [
                ("connection_guides", "JSON آموزش‌ها (از وب‌پنل)", "textarea"),
            ]),
        ],
    },
    "notify": {
        "title": "اعلان‌ها",
        "kind": "notify",
    },
    # Own shop bot token only — never platform BOT_TOKEN.
    "bot": {
        "title": "ربات",
        "kind": "bot",
    },
}

HUB_ORDER = ["shop", "menu", "pay", "support", "access", "limits", "guides", "notify", "bot"]


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
    "services": "سرویس‌ها",
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
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
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
        return None, "فقط نمایندگان"
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


async def _render_hub(
    callback: CallbackQuery,
    session: AsyncSession,
    profile,
    db_user: BotUser | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    refresh_keyboard: bool = False,
):
    """Hub tip under the message; lasting reply chrome healed on cancel/save."""
    bot_line = f"@{profile.bot_username}" if profile.bot_username else "توکن ثبت نشده"
    text = (
        "⚙️ <b>تنظیمات سریع فروشگاه</b>\n\n"
        f"ربات: <code>{bot_line}</code>\n"
        "بخش را از دکمه‌های پنل اینلاین انتخاب کنید.\n"
        "فقط محدودهٔ همین فروشگاه — بدون تنظیمات پلتفرم.\n"
        "ظاهر، رنگ، گزارش روزانه → وب‌پنل."
    )
    rows: list[list[InlineKeyboardButton]] = []
    from app.services.resellers import get_reseller_panel_base_url

    base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
    if base.startswith("http"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌐 تنظیمات کامل در وب‌پنل",
                    url=f"{base}/shop-settings",
                )
            ]
        )
    markup = _kb(rows) if rows else None
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)
        if refresh_keyboard and db_user is not None:
            try:
                await callback.message.answer(
                    "کیبورد اصلی:",
                    reply_markup=await _lasting_kb(
                        session,
                        db_user,
                        kb.reseller_settings_reply_keyboard(),
                        is_reseller_bot=is_reseller_bot,
                        reseller_owner_id=reseller_owner_id,
                    ),
                )
            except Exception:
                pass


async def _render_section(callback: CallbackQuery, session: AsyncSession, sec_id: str, reseller_id: int):
    sec = SECTIONS.get(sec_id)
    if not sec:
        await callback.answer("نامعتبر", show_alert=True)
        return
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
            rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="res:st:hub")])
            body = "پشتیبان فعال نیست." if not contacts else "برای حذف روی مورد بزنید."
            if callback.message:
                await safe_edit_text(
                    callback.message,
                    f"💬 <b>پشتیبان‌ها</b>\n\n{body}",
                    reply_markup=_kb(rows),
                )
            return

        if sec.get("kind") == "bot":
            profile = await get_reseller_profile(session, reseller_id)
            uname = f"@{profile.bot_username}" if profile and profile.bot_username else "—"
            rows = [
                [InlineKeyboardButton(text="🔄 تغییر توکن ربات", callback_data="res:st:bot:token")],
                [InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="res:st:hub")],
            ]
            text = (
                "🤖 <b>ربات فروشگاه</b>\n\n"
                f"یوزرنیم: <code>{uname}</code>\n"
                "فقط توکن <b>همین فروشگاه</b> را از @BotFather بگیرید.\n"
                "توکن ربات اصلی/پلتفرم اینجا کار نمی‌کند و ذخیره نمی‌شود به‌عنوان پلتفرم.\n"
                "پس از تغییر، ربات ظرف چند ثانیه وصل می‌شود."
            )
            if callback.message:
                await safe_edit_text(callback.message, text, reply_markup=_kb(rows))
            return

        if sec.get("kind") == "notify":
            await _render_notify(callback, session, reseller_id)
            return

        rows = []
        for sub_id, label, _fields in sec.get("subs") or []:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"res:st:sub:{sec_id}:{sub_id}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="res:st:hub")])
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"⚙️ <b>{sec['title']}</b>",
                reply_markup=_kb(rows),
            )


async def _render_notify(callback: CallbackQuery, session: AsyncSession, reseller_id: int):
    from app.services.authz import resolve_shop_permissions_from_profile
    from app.services.notifications import get_shop_notify_prefs, shop_notify_catalog

    profile = await get_reseller_profile(session, reseller_id)
    perms = list(resolve_shop_permissions_from_profile(profile) or [])
    catalog = shop_notify_catalog(perms)
    prefs = await get_shop_notify_prefs(session, reseller_id)
    rows: list[list[InlineKeyboardButton]] = []
    for key, title, _, _default in catalog:
        mark = "✅" if prefs.get(key) else "⬜️"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"res:st:ntog:{key}")]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="— اعلانی برای دسترسی شما نیست —", callback_data="res:st:hub")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ تنظیمات", callback_data="res:st:hub")])
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
    sec = SECTIONS.get(sec_id) or {}
    fields = None
    title = sub_id
    for sid, label, payload in sec.get("subs") or []:
        if sid == sub_id:
            title = label
            fields = payload
            break
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
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"res:st:sec:{sec_id}")],
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
    if not isinstance(fields, list):
        await callback.answer("نامعتبر", show_alert=True)
        return
    with _Scoped(reseller_id):
        rows = []
        for key, label, kind in fields:
            cur = await get_setting(session, key, reseller_id=reseller_id)
            if kind == "toggle":
                mark = "✅" if on(cur) else "❌"
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{mark} {label}",
                            callback_data=f"res:st:tog:{key}",
                        )
                    ]
                )
            else:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"✏️ {label}: {_preview(cur, limit=28)}",
                            callback_data=f"res:st:edit:{key}",
                        )
                    ]
                )
        rows.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"res:st:sec:{sec_id}")]
        )
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"⚙️ <b>{title}</b>",
                reply_markup=_kb(rows),
            )


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
        await callback.answer("نامعتبر", show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, key, reseller_id=profile.user_id)
        new_val = "0" if on(cur) else "1"
        await set_setting(session, key, new_val, reseller_id=profile.user_id)
    await callback.answer("ذخیره شد")
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
    await callback.answer("ذخیره شد")
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
    await callback.answer("ذخیره شد")
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
    await callback.answer("ذخیره شد")
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
        await callback.answer("نامعتبر", show_alert=True)
        return
    with _Scoped(profile.user_id):
        cur = await get_setting(session, key, reseller_id=profile.user_id)
    if key == "force_join_channel":
        from app.services.users import format_force_join_for_edit

        cur = format_force_join_for_edit(cur)
    await callback.answer()
    await state.set_state(ResellerSettingsStates.edit_value)
    await state.update_data(edit_key=key, reseller_id=profile.user_id)
    hint = "متن جدید را بفرستید.\nبرای انصراف: انصراف"
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
            f"<b>{meta[1]}</b>\nفعلی:\n<code>{_preview(cur)}</code>\n\n{hint}",
            reply_markup=kb.cancel_reply(),
        )


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
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=await _lasting_kb(session, db_user, kb.reseller_settings_reply_keyboard(), is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id),
        )
        return
    if key == "force_join_channel":
        from app.services.users import normalize_force_join_channel_value

        text = normalize_force_join_channel_value(text)
    from app.services.rich_text import pack_setting_from_message

    text = pack_setting_from_message(key, message)
    await set_setting(session, key, text, reseller_id=profile.user_id)
    await state.clear()
    await message.answer(
        "✅ ذخیره شد.",
        reply_markup=await _lasting_kb(session, db_user, kb.reseller_settings_reply_keyboard(), is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id),
    )


@router.callback_query(F.data == "res:st:sup:add")
async def support_add(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None):
    profile, err = await _gate(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.answer()
    await state.set_state(ResellerSettingsStates.support_title)
    await state.update_data(reseller_id=profile.user_id)
    if callback.message:
        await callback.message.answer("عنوان پشتیبان را بفرستید:", reply_markup=kb.cancel_reply())


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
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _lasting_kb(session, db_user, kb.reseller_settings_reply_keyboard(), is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id))
        return
    await state.update_data(support_title=text)
    await state.set_state(ResellerSettingsStates.support_telegram)
    await message.answer("یوزرنیم یا آیدی عددی تلگرام را بفرستید:", reply_markup=kb.cancel_reply())


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
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _lasting_kb(session, db_user, kb.reseller_settings_reply_keyboard(), is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id))
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
    await state.clear()
    if err2:
        await message.answer(f"❌ {err2}")
        return
    await message.answer(
        "✅ پشتیبان اضافه شد.",
        reply_markup=_kb([[InlineKeyboardButton(text="💬 پشتیبان‌ها", callback_data="res:st:sec:support")]]),
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
    await callback.answer("حذف شد")
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
    await state.update_data(reseller_id=profile.user_id)
    if callback.message:
        await callback.message.answer(
            "توکن جدید ربات را از @BotFather بفرستید:\nبرای انصراف: انصراف",
            reply_markup=kb.cancel_reply(),
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
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _lasting_kb(session, db_user, kb.reseller_settings_reply_keyboard(), is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id))
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
        await message.answer(f"❌ {e}")
        return
    except Exception:
        await message.answer("❌ توکن نامعتبر است")
        return
    await state.clear()
    await message.answer(
        f"✅ ربات @{uname} ثبت و راه‌اندازی شد.",
        reply_markup=_kb([[InlineKeyboardButton(text="⚙️ تنظیمات", callback_data="res:st:hub")]]),
    )
