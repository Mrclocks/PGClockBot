"""Customer freeze of a service: choose the days, confirm, and resume early from «⚙️ بیشتر»."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, UserService
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message
from app.services.numbers import to_fa_digits
from app.services.redact import user_safe_error
from app.services.service_freeze import resume_freeze, start_freeze
from app.services.service_freeze_settings import STATUS_DONE, STATUS_FROZEN, freeze_policy
from app.services.service_scope import customer_may_use_service
from app.services.users import current_shop_reseller_id, get_all_settings

router = Router(name="service_freeze")

ASK_TEXT = (
    "سرویس <b>{name}</b> چند روز متوقف شود؟\n"
    "در این مدت سرویس وصل نمی‌شود و روزهای باقی‌مانده‌اش حفظ می‌شود. هر وقت بخواهید با «▶️ ادامهٔ سرویس» "
    "از همان «⚙️ بیشتر» دوباره فعالش کنید؛ در پایان مهلت هم خودکار فعال می‌شود."
)


def _back(service_id: int) -> list[InlineKeyboardButton]:
    return [kb.back_button(None, f"svc:more:{int(service_id)}")]


async def _service_for(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, service_id: int) -> UserService | None:
    service = await session.get(UserService, service_id)
    if not await customer_may_use_service(session, service, db_user.id, current_shop_reseller_id()):
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return None
    return service


@router.callback_query(F.data.regexp(r"^svc:frz:\d{1,10}$"))
async def freeze_ask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = int((callback.data or "").rsplit(":", 1)[-1])
    service = await _service_for(callback, session, db_user, service_id)
    if service is None:
        return
    policy = freeze_policy(await get_all_settings(session))
    if not policy.enabled:
        await callback.answer("توقف موقت سرویس در این فروشگاه فعال نیست", show_alert=True)
        return
    await callback.answer()
    days = [
        InlineKeyboardButton(text=f"{to_fa_digits(d)} روز", callback_data=f"svc:frzd:{service_id}:{d}")
        for d in policy.day_choices()
    ]
    rows = [days[i:i + 2] for i in range(0, len(days), 2)] + [_back(service_id)]
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("⏸ توقف موقت سرویس", ASK_TEXT.format(name=html.escape(service.pg_username or f"#{service_id}"))),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.regexp(r"^svc:frzd:\d{1,10}:\d{1,3}$"))
async def freeze_start(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    _, _, raw_service, raw_days = (callback.data or "").split(":")
    service_id = int(raw_service)
    if await _service_for(callback, session, db_user, service_id) is None:
        return
    try:
        row = await start_freeze(
            session, db_user, service_id, shop_id=current_shop_reseller_id(), days=int(raw_days),
            ui=await get_all_settings(session),
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    body = (
        f"سرویس متوقف شد و تا {to_fa_digits(raw_days)} روز دیگر روزهای باقی‌مانده‌اش حفظ می‌شود."
        if row.status == STATUS_FROZEN
        else "درخواست توقف ثبت شد و تا چند دقیقهٔ دیگر روی سرویس اعمال می‌شود."
    )
    if callback.message:
        await safe_edit_text(
            callback.message, format_message("⏸ توقف موقت سرویس", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back(service_id)]),
        )


@router.callback_query(F.data.regexp(r"^svc:frzres:\d{1,10}$"))
async def freeze_resume(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    service_id = int((callback.data or "").rsplit(":", 1)[-1])
    if await _service_for(callback, session, db_user, service_id) is None:
        return
    try:
        row = await resume_freeze(session, db_user, service_id, shop_id=current_shop_reseller_id())
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    body = (
        "سرویس دوباره فعال شد و روزهای باقی‌مانده‌اش از همین حالا ادامه پیدا می‌کند."
        if row.status == STATUS_DONE
        else "فعال شدن سرویس ثبت شد و تا چند دقیقهٔ دیگر روی سرویس اعمال می‌شود."
    )
    if callback.message:
        await safe_edit_text(
            callback.message, format_message("▶️ ادامهٔ سرویس", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back(service_id)]),
        )
