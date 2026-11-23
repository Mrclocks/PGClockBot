"""Customer transfer of a service: make or cancel the one-time link, and accept it from /start."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, UserService
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message
from app.services.numbers import to_fa_digits
from app.services.redact import user_safe_error
from app.services.service_scope import customer_may_use_service
from app.services.service_transfer import accept_transfer, cancel_transfer, create_transfer, preview_transfer
from app.services.service_transfer_settings import DEEPLINK_PREFIX, transfer_policy
from app.services.users import current_shop_reseller_id, get_all_settings

router = Router(name="service_transfer")

TITLE = "🔁 انتقال سرویس"
EXPLAIN = (
    "سرویس <b>{name}</b> به شخص دیگری منتقل شود؟\n\n"
    "یک لینک یک‌بارمصرف ساخته می‌شود که {hours} ساعت اعتبار دارد. هر کس آن را در همین ربات باز کند و بپذیرد، "
    "صاحب سرویس می‌شود و سرویس از «سرویس‌های من» شما برداشته می‌شود. تمدید خودکار سرویس خاموش می‌شود."
)
LINK_TEXT = (
    "لینک انتقال ساخته شد. آن را فقط برای کسی بفرستید که سرویس را به او می‌دهید:\n\n<code>{link}</code>\n\n"
    "تا وقتی پذیرفته نشده، می‌توانید از «⚙️ بیشتر» لینک را لغو کنید."
)
OFFER_TEXT = (
    "سرویس <b>{name}</b> برای شما فرستاده شده است.\n"
    "با پذیرفتن، این سرویس به «سرویس‌های من» شما اضافه می‌شود و لینک اشتراک جدیدش را از همان‌جا می‌گیرید."
)


def _back(service_id: int) -> list[InlineKeyboardButton]:
    return [kb.back_button(None, f"svc:more:{int(service_id)}")]


async def _service_for(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, service_id: int) -> UserService | None:
    service = await session.get(UserService, service_id)
    if not await customer_may_use_service(session, service, db_user.id, current_shop_reseller_id()):
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return None
    return service


async def _edit(callback: CallbackQuery, body: str, rows: list[list[InlineKeyboardButton]]) -> None:
    if callback.message:
        await safe_edit_text(callback.message, format_message(TITLE, body), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^svc:trn:\d{1,10}$"))
async def transfer_ask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = int((callback.data or "").rsplit(":", 1)[-1])
    service = await _service_for(callback, session, db_user, service_id)
    if service is None:
        return
    policy = transfer_policy(await get_all_settings(session))
    if not policy.enabled:
        await callback.answer("انتقال سرویس در این فروشگاه فعال نیست", show_alert=True)
        return
    await callback.answer()
    name = html.escape(service.pg_username or f"#{service_id}")
    await _edit(callback, EXPLAIN.format(name=name, hours=to_fa_digits(policy.link_hours)), [
        [InlineKeyboardButton(text="🔗 ساخت لینک انتقال", callback_data=f"svc:trnmk:{service_id}")], _back(service_id),
    ])


@router.callback_query(F.data.regexp(r"^svc:trnmk:\d{1,10}$"))
async def transfer_make(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = int((callback.data or "").rsplit(":", 1)[-1])
    if await _service_for(callback, session, db_user, service_id) is None:
        return
    try:
        _row, token = await create_transfer(
            session, db_user, service_id, shop_id=current_shop_reseller_id(), ui=await get_all_settings(session)
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    me = await callback.bot.me()
    link = f"https://t.me/{me.username}?start={DEEPLINK_PREFIX}{token}"
    await _edit(callback, LINK_TEXT.format(link=html.escape(link)), [_back(service_id)])


@router.callback_query(F.data.regexp(r"^svc:trnx:\d{1,10}$"))
async def transfer_cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = int((callback.data or "").rsplit(":", 1)[-1])
    if await _service_for(callback, session, db_user, service_id) is None:
        return
    try:
        cancelled = await cancel_transfer(session, db_user, service_id, shop_id=current_shop_reseller_id())
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    body = "لینک انتقال لغو شد و دیگر کار نمی‌کند." if cancelled else "لینک انتقال بازی برای این سرویس نبود."
    await _edit(callback, body, [_back(service_id)])


async def present_transfer_offer(message: Message, session: AsyncSession, db_user: BotUser, token: str) -> None:
    """The confirmation a recipient sees after opening a transfer link."""
    try:
        offer = await preview_transfer(session, db_user, token, shop_id=current_shop_reseller_id())
    except ValueError as exc:
        await message.answer(format_message(TITLE, html.escape(user_safe_error(exc))))
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ می‌پذیرم", callback_data=f"svctr:ok:{token}")],
        [InlineKeyboardButton(text="نمی‌خواهم", callback_data="svctr:no")],
    ])
    await message.answer(format_message(TITLE, OFFER_TEXT.format(name=html.escape(offer.service_name))), reply_markup=markup)


@router.callback_query(F.data.regexp(r"^svctr:ok:[A-Za-z0-9_-]{16,43}$"))
async def transfer_accept(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    token = (callback.data or "").split(":", 2)[2]
    try:
        service = await accept_transfer(
            session, db_user, token, shop_id=current_shop_reseller_id(), ui=await get_all_settings(session)
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    body = f"سرویس <b>{html.escape(service.pg_username or '')}</b> به «سرویس‌های من» شما اضافه شد."
    await _edit(callback, body, [[InlineKeyboardButton(text="📦 سرویس‌های من", callback_data="svc:list")]])


@router.callback_query(F.data == "svctr:no")
async def transfer_decline(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit(callback, "انتقال را نپذیرفتید؛ سرویس نزد صاحب قبلی می‌ماند.", [])
