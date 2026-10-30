"""Bot flow that voids an approved payment: list -> card -> reason -> confirm.

Every step re-resolves the actor's shop and re-checks the permission; the payment id in the
callback is only a lookup key inside that shop. The reasons are fixed texts, so there is no
free-text step.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_toman, order_status_fa
from app.services.payment_voids import (
    BOT_REASONS,
    STATUS_LABELS,
    list_void_candidates,
    load_void_candidate,
    offers_void,
    void_and_notify,
)
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.users import get_all_settings

router = Router(name="payment_voids")

PERMISSION = "payment_void"
PLATFORM_BACK = "adm:orders"
SHOP_BACK = "res:orders"
ID = r"(\d{1,12})"


async def void_scope(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> tuple[bool, int | None]:
    """(allowed, shop_id): the shop owner with the permission, or the explicit platform Owner."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile:
        return has_bot_perm(profile, PERMISSION), owner_id
    if is_reseller_bot:
        return False, None
    return await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False), None


def void_button(payment_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="↩️ ابطال پرداخت", callback_data=f"payvoid:v:{int(payment_id)}")


def _money(amount: int) -> str:
    return format_toman(amount, get_settings().currency)


def _customer_name(user: BotUser | None, fallback: int) -> str:
    return html.escape((user.full_name or user.username or str(fallback)) if user else str(fallback))


def _back(shop_id: int | None) -> str:
    return PLATFORM_BACK if shop_id is None else SHOP_BACK


async def _gate(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, **ctx) -> int | None | bool:
    """The caller's shop id (None = platform), or False after answering a denial."""
    allowed, shop_id = await void_scope(session, db_user, **ctx)
    if not allowed:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return False
    return shop_id


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data == "payvoid:list")
async def void_list(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    await callback.answer()
    rows = []
    for payment, _order, user, void in await list_void_candidates(session, shop_id=shop_id):
        mark = "⚠ " if void is not None else ""
        label = f"{mark}#{payment.id} · {_money(payment.amount)} · {(user.full_name or user.username or '') if user else ''}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"payvoid:v:{payment.id}")])
    text = (
        "↩️ <b>ابطال پرداخت تأییدشده</b>\nپرداختی را انتخاب کنید:\n<i>⚠ = ابطال ناتمام (نیاز به تلاش مجدد)</i>"
        if rows else "پرداخت تأییدشدهٔ قابل ابطالی نیست."
    )
    await _edit(callback, session, text, rows, _back(shop_id))


async def _show_card(callback: CallbackQuery, session: AsyncSession, shop_id: int | None, payment_id: int) -> None:
    try:
        payment, order, void = await load_void_candidate(session, payment_id, shop_id=shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    user = await session.get(BotUser, payment.user_id)
    state = STATUS_LABELS[void.status] if void is not None else "تأییدشده"
    lines = [
        f"↩️ <b>پرداخت #{payment.id}</b>",
        f"وضعیت: <b>{state}</b>",
        f"مبلغ: {_money(payment.amount)}",
        f"مشتری: {_customer_name(user, payment.user_id)}",
    ]
    if order is not None:
        lines.append(f"سفارش: #{order.id} ({order_status_fa(order.status)})")
    rows: list[list[InlineKeyboardButton]] = []
    if void is not None and void.status != "done":
        lines.append("\nپنل تأیید نکرد؛ سرویس هنوز فعال است و مبلغی برنگشته است.")
        rows.append([InlineKeyboardButton(text="🔁 ادامهٔ ابطال", callback_data=f"payvoid:retry:{payment.id}")])
    elif offers_void(payment, order):
        rows.append([InlineKeyboardButton(text="↩️ ابطال پرداخت", callback_data=f"payvoid:ask:{payment.id}")])
    await _edit(callback, session, "\n".join(lines), rows, "payvoid:list")


@router.callback_query(F.data.regexp(rf"^payvoid:v:{ID}$"))
async def void_card(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    await callback.answer()
    await _show_card(callback, session, shop_id, int(callback.data.split(":")[2]))


@router.callback_query(F.data.regexp(rf"^payvoid:ask:{ID}$"))
async def void_ask_reason(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    payment_id = int(callback.data.split(":")[2])
    try:
        payment, order, void = await load_void_candidate(session, payment_id, shop_id=shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if void is not None or not offers_void(payment, order):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    rows = [
        [InlineKeyboardButton(text=text, callback_data=f"payvoid:r:{payment_id}:{key}")]
        for key, text in BOT_REASONS.items()
    ]
    await _edit(callback, session, f"دلیل ابطال پرداخت #{payment_id} را انتخاب کنید:", rows, f"payvoid:v:{payment_id}")


@router.callback_query(F.data.regexp(rf"^payvoid:r:{ID}:([1-9])$"))
async def void_confirm(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    _, _, raw_id, key = callback.data.split(":")
    payment_id = int(raw_id)
    if key not in BOT_REASONS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        payment, order, void = await load_void_candidate(session, payment_id, shop_id=shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if void is not None or not offers_void(payment, order):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    text = (
        f"⚠️ <b>ابطال پرداخت #{payment_id}</b>\n"
        f"دلیل: {html.escape(BOT_REASONS[key])}\n\n"
        "سرویس این سفارش در پنل غیرفعال و سفارش لغو می‌شود. این کار برگشت‌پذیر نیست.\n"
        f"مبلغ {_money(payment.amount)} به کیف پول مشتری برگردد؟"
    )
    rows = [
        [InlineKeyboardButton(text="ابطال بدون بازگشت وجه", callback_data=f"payvoid:go:{payment_id}:{key}:0")],
        [InlineKeyboardButton(text="ابطال و بازگشت وجه به کیف", callback_data=f"payvoid:go:{payment_id}:{key}:1")],
    ]
    await _edit(callback, session, text, rows, f"payvoid:ask:{payment_id}")


async def _run(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, shop_id: int | None,
    payment_id: int, *, reason: str, refund: bool,
) -> None:
    try:
        await void_and_notify(
            session, payment_id, shop_id=shop_id, actor=f"tg:{db_user.telegram_id}", reason=reason, refund_to_wallet=refund
        )
    except ValueError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        await _show_card(callback, session, shop_id, payment_id)
        return
    await callback.answer("پرداخت ابطال شد", show_alert=True)
    await _edit(callback, session, f"✅ پرداخت #{payment_id} ابطال شد.", [], "payvoid:list")


@router.callback_query(F.data.regexp(rf"^payvoid:go:{ID}:([1-9]):[01]$"))
async def void_go(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    _, _, raw_id, key, refund = callback.data.split(":")
    if key not in BOT_REASONS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await _run(callback, session, db_user, shop_id, int(raw_id), reason=BOT_REASONS[key], refund=refund == "1")


@router.callback_query(F.data.regexp(rf"^payvoid:retry:{ID}$"))
async def void_retry(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    payment_id = int(callback.data.split(":")[2])
    try:
        _, _, void = await load_void_candidate(session, payment_id, shop_id=shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if void is None or void.status == "done":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await _run(callback, session, db_user, shop_id, payment_id, reason=void.reason, refund=bool(void.refund_amount))
