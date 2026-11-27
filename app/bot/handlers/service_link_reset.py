"""Customer change of a service's subscription link from «⚙️ بیشتر»: warn, confirm, show the new link."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, UserService
from app.services import bot_feedback_texts as feedback
from app.services.formatting import copyable, format_message
from app.services.numbers import to_fa_digits
from app.services.redact import user_safe_error
from app.services.service_link_reset import changes_left, link_reset_button, start_link_reset
from app.services.service_link_reset_settings import STATUS_DONE, link_reset_policy
from app.services.service_scope import customer_may_use_service
from app.services.users import current_shop_reseller_id, get_all_settings

router = Router(name="service_link_reset")

TITLE = "🔗 تغییر لینک اشتراک"
ASK_TEXT = (
    "لینک اشتراک سرویس <b>{name}</b> عوض شود؟\n\n"
    "⚠️ لینک فعلی همین لحظه از کار می‌افتد و همهٔ دستگاه‌هایی که با آن وصل‌اند قطع می‌شوند. "
    "لینک جدید را باید روی هر دستگاه دوباره وارد کنید.\n"
    "این کار را وقتی انجام دهید که لینکتان به دست کس دیگری افتاده است.\n\n"
    "در ۳۰ روز گذشته {left} بار دیگر می‌توانید لینک این سرویس را عوض کنید."
)
DONE_TEXT = "✅ لینک اشتراک عوض شد. لینک قبلی دیگر کار نمی‌کند.\n\nلینک جدید:\n{link}"
UNKNOWN_TEXT = (
    "⏳ درخواست به سرور رسید ولی پاسخش هنوز نیامده. تا چند دقیقهٔ دیگر نتیجه مشخص می‌شود؛ "
    "بعد لینک جدید را از «🔗 لینک و QR» بگیرید. لطفاً دوباره دکمه را نزنید."
)
CONFIRM_TEXT = "✅ بله، لینک را عوض کن"
CONNECT_AGAIN_TEXT = "🔗 لینک و QR"


def _service_id(callback: CallbackQuery) -> int:
    return int((callback.data or "").rsplit(":", 1)[-1])


def _back(service_id: int) -> list[InlineKeyboardButton]:
    return [kb.back_button(None, f"svc:more:{int(service_id)}")]


async def _service_for(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, service_id: int
) -> UserService | None:
    service = await session.get(UserService, service_id)
    if not await customer_may_use_service(session, service, db_user.id, current_shop_reseller_id()):
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return None
    return service


@router.callback_query(F.data.regexp(r"^svc:lnk:\d{1,10}$"))
async def link_reset_ask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = _service_id(callback)
    service = await _service_for(callback, session, db_user, service_id)
    if service is None:
        return
    ui = await get_all_settings(session)
    if not await link_reset_button(session, service, ui):
        await callback.answer("تغییر لینک برای این سرویس فعال نیست", show_alert=True)
        return
    left = await changes_left(session, service_id, link_reset_policy(ui), datetime.now(timezone.utc))
    await callback.answer()
    name = html.escape(service.pg_username or f"#{service_id}")
    rows = [[InlineKeyboardButton(text=CONFIRM_TEXT, callback_data=f"svc:lnkok:{service_id}")], _back(service_id)]
    if callback.message:
        await safe_edit_text(
            callback.message, format_message(TITLE, ASK_TEXT.format(name=name, left=to_fa_digits(left))),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.regexp(r"^svc:lnkok:\d{1,10}$"))
async def link_reset_confirm(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = _service_id(callback)
    if await _service_for(callback, session, db_user, service_id) is None:
        return
    try:
        outcome = await start_link_reset(
            session, db_user, service_id, shop_id=current_shop_reseller_id(), ui=await get_all_settings(session),
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if outcome.status == STATUS_DONE and outcome.link is not None:
        body = DONE_TEXT.format(link=copyable(outcome.link.url))
    else:
        body = UNKNOWN_TEXT
    rows = [
        [InlineKeyboardButton(text=CONNECT_AGAIN_TEXT, callback_data=f"svc:link:{service_id}")],
        [InlineKeyboardButton(text="📘 آموزش اتصال", callback_data=f"guide:svc:{service_id}")],
        _back(service_id),
    ]
    if callback.message:
        await safe_edit_text(
            callback.message, format_message(TITLE, body), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
