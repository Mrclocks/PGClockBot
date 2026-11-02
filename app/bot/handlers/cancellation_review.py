"""Approve or reject a service cancellation request from the bot (same rules as the web tab).

The cancellation card is rendered here for both the platform Owner and a shop operator. Every
step re-resolves the actor's shop and permission; the request id in a callback is only a lookup
key inside that shop. Approving a pending request asks the operator for the refund amount, and
a request left in review reuses the amount saved by the first attempt.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import require_bot_owner
from app.bot.nav_inline import with_inline_back
from app.bot.nav_input import CancelEntry, ask_text, finish_text_step, register_cancel_code, try_legacy_cancel
from app.bot.tg_utils import parse_bot_int, safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, ServiceCancellation
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_toman
from app.services.numbers import normalize_number_text
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.service_cancellations import (
    STATUS_LABELS,
    approve_cancellation,
    assert_cancellation_scope,
    reject_cancellation,
)
from app.services.users import get_all_settings

router = Router(name="cancellation_review")

CANCEL_CODE = "cn_rev"
APPROVE_PERMISSIONS = ("orders", "payments")
REJECT_PERMISSIONS = ("orders",)
MAX_REFUND = 2**31 - 1
REJECT_NOTES = {
    "1": "درخواست با شرایط لغو فروشگاه مطابقت ندارد.",
    "2": "سرویس هنوز قابل استفاده است؛ در صورت نیاز با پشتیبانی تماس بگیرید.",
    "3": "این درخواست تکراری است.",
}
OPEN_FOR_APPROVAL = ("pending", "processing", "review")
ID = r"(\d{1,12})"
PLATFORM_LIST = "adm:cancellations"
SHOP_LIST = "res:cancellations"


class CancelReviewStates(StatesGroup):
    amount = State()


async def review_scope(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool,
    reseller_owner_id: int | None, permissions: tuple[str, ...],
) -> tuple[bool, int | None]:
    """(allowed, shop_id): the shop operator holding every permission, or the explicit platform Owner."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile:
        return all(has_bot_perm(profile, key) for key in permissions), owner_id
    if is_reseller_bot:
        return False, None
    return await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False), None


def _list_back(shop_id: int | None) -> str:
    return PLATFORM_LIST if shop_id is None else SHOP_LIST


def _view(request_id: int) -> str:
    return f"cnrv:v:{int(request_id)}"


async def show_card(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, request_id: int, *,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None, answer: bool = True,
) -> None:
    """Render the request card; each decision button appears only when the actor holds its permission.

    ``answer`` acknowledges the tap once the actor and request are validated; callers that already
    answered with an alert pass False, since a callback can be answered only once.
    """
    ctx = {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id}
    can_reject, shop_id = await review_scope(session, db_user, permissions=REJECT_PERMISSIONS, **ctx)
    if not can_reject:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    can_approve, _ = await review_scope(session, db_user, permissions=APPROVE_PERMISSIONS, **ctx)
    row = await session.get(ServiceCancellation, int(request_id))
    try:
        assert_cancellation_scope(row, shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if answer:
        await callback.answer()
    text = (
        f"📝 <b>درخواست لغو #{row.id}</b>\n"
        f"سرویس: <code>{row.service_id}</code>\n"
        f"وضعیت: {html.escape(STATUS_LABELS.get(row.status, row.status))}\n"
        f"دلیل:\n{html.escape((row.reason or '')[:800])}"
    )
    if row.refund_amount is not None and row.status != "pending":
        text += f"\nاعتبار تعیین‌شده: {format_toman(row.refund_amount, get_settings().currency)}"
    rows: list[list[InlineKeyboardButton]] = []
    if row.status == "pending":
        decisions = []
        if can_approve:
            decisions.append(InlineKeyboardButton(text="✅ تأیید و تعیین اعتبار", callback_data=f"cnrv:a:{row.id}"))
        decisions.append(InlineKeyboardButton(text="❌ رد", callback_data=f"cnrv:r:{row.id}"))
        rows.append(decisions)
    elif can_approve and row.status in OPEN_FOR_APPROVAL:
        rows.append([InlineKeyboardButton(text="🔁 بررسی پنل و ادامه", callback_data=f"cnrv:a:{row.id}")])
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, _list_back(shop_id))
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


async def _gate(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, permissions: tuple[str, ...], **ctx
) -> int | None | bool:
    """The caller's shop id (None = platform), or False after answering a denial."""
    allowed, shop_id = await review_scope(session, db_user, permissions=permissions, **ctx)
    if not allowed:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return False
    return shop_id


@router.callback_query(F.data.regexp(rf"^cnrv:a:{ID}$"))
async def approve_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, APPROVE_PERMISSIONS, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    request_id = int(callback.data.split(":")[2])
    row = await session.get(ServiceCancellation, request_id)
    try:
        assert_cancellation_scope(row, shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if row.status not in OPEN_FOR_APPROVAL:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    if row.refund_amount is not None:
        # A retry must keep the amount the first attempt saved.
        text, markup = await _confirm_view(session, request_id, int(row.refund_amount))
        if callback.message:
            await safe_edit_text(callback.message, text, reply_markup=markup)
        return
    await state.update_data(cnrv_id=request_id, cnrv_shop=shop_id)
    await ask_text(
        callback, state,
        prompt=f"💰 مبلغ اعتبار برگشتی درخواست #{request_id} را به تومان بفرستید (۰ = بدون اعتبار):",
        cancel_code=CANCEL_CODE, fsm_state=CancelReviewStates.amount, return_to=_view(request_id), edit=True,
    )


async def _confirm_view(session: AsyncSession, request_id: int, amount: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"⚠️ <b>تأیید لغو #{request_id}</b>\n"
        f"اعتبار برگشتی: <b>{format_toman(amount, get_settings().currency)}</b>\n\n"
        "سرویس در پنل غیرفعال می‌شود و اعتبار به کیف پول مشتری در همین فروشگاه واریز می‌شود."
    )
    rows = [[InlineKeyboardButton(text="✅ تأیید و واریز", callback_data=f"cnrv:ok:{request_id}:{amount}")]]
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), _view(request_id))
    return text, markup


@router.message(CancelReviewStates.amount)
async def approve_amount(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text) and await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    allowed, shop_id = await review_scope(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
        permissions=APPROVE_PERMISSIONS,
    )
    data = await state.get_data()
    request_id = int(data.get("cnrv_id") or 0)
    if not allowed or int(data.get("cnrv_shop") or 0) != int(shop_id or 0):
        await state.clear()
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    raw = normalize_number_text(message.text or "")
    try:
        amount = parse_bot_int(raw)
    except ValueError:
        amount = -1
    if not raw.isdigit() or not 0 <= amount <= MAX_REFUND:
        await message.answer("مبلغ را فقط با ارقام و به تومان بفرستید.")
        return
    await state.set_state(None)
    text, markup = await _confirm_view(session, request_id, amount)
    await finish_text_step(message, state, text=text, inline=markup)


@router.callback_query(F.data.regexp(rf"^cnrv:ok:{ID}:(\d{{1,10}})$"))
async def approve_run(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, APPROVE_PERMISSIONS, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    _, _, raw_id, raw_amount = callback.data.split(":")
    request_id, amount = int(raw_id), int(raw_amount)
    try:
        await approve_cancellation(session, request_id, shop_id=shop_id, amount=amount, actor=f"tg:{db_user.telegram_id}")
    except ValueError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        await show_card(callback, session, db_user, request_id, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id, answer=False)
        return
    await callback.answer("لغو تأیید شد", show_alert=True)
    await show_card(callback, session, db_user, request_id, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id, answer=False)


@router.callback_query(F.data.regexp(rf"^cnrv:r:{ID}$"))
async def reject_ask(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, REJECT_PERMISSIONS, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    request_id = int(callback.data.split(":")[2])
    row = await session.get(ServiceCancellation, request_id)
    try:
        assert_cancellation_scope(row, shop_id)
    except ValueError:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if row.status != "pending":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    rows = [
        [InlineKeyboardButton(text=text, callback_data=f"cnrv:rn:{request_id}:{key}")]
        for key, text in REJECT_NOTES.items()
    ]
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, _view(request_id))
    if callback.message:
        await safe_edit_text(callback.message, f"دلیل رد درخواست #{request_id} (به مشتری نمایش داده می‌شود):", reply_markup=markup)


@router.callback_query(F.data.regexp(rf"^cnrv:rn:{ID}:([1-9])$"))
async def reject_run(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, REJECT_PERMISSIONS, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    _, _, raw_id, key = callback.data.split(":")
    request_id = int(raw_id)
    if key not in REJECT_NOTES:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        await reject_cancellation(session, request_id, shop_id=shop_id, actor=f"tg:{db_user.telegram_id}", note=REJECT_NOTES[key])
    except ValueError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        await show_card(callback, session, db_user, request_id, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id, answer=False)
        return
    await callback.answer("درخواست رد شد", show_alert=True)
    await show_card(callback, session, db_user, request_id, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id, answer=False)


@router.callback_query(F.data.regexp(rf"^cnrv:v:{ID}$"))
async def view_card(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    await state.set_state(None)
    await show_card(
        callback, session, db_user, int(callback.data.split(":")[2]),
        is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
    )


async def _reopen_after_cancel(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_after_cancel))
