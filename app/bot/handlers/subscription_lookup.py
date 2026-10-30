"""Staff search by subscription link, name, @username or id: buttons into the existing cards.

The platform Owner reaches it from the users search (see ``adm_users_search``); a shop operator
with ``dashboard`` from the customers list. Both are scoped to the shop that sold the service, and
a link of another shop answers exactly like an unknown one. The typed link is deleted afterwards.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_inline import with_inline_back
from app.bot.nav_input import CancelEntry, ask_text, finish_text_step, register_cancel_code, try_legacy_cancel
from app.config import get_settings
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_bytes, format_expire_short, format_toman
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.subscription_lookup import SubscriptionMatch, find_service_by_link, link_token
from app.services.user_search import MIN_QUERY_LENGTH, clean_handle, search_bot_users
from app.services.users_ops import scoped_users_where
from app.services.users import get_all_settings
from app.services.wallet import get_wallet_balance

logger = logging.getLogger(__name__)
router = Router(name="subscription_lookup")

CANCEL_CODE = "sub_lk"
PERMISSION = "dashboard"
PLATFORM_BACK = "adm:users"
SHOP_BACK = "res:users:0"
ASK_CALLBACK = "sublk:ask"
PLATFORM_ASK = "adm:users:search"
NOT_FOUND_TEXT = "اشتراکی با این لینک در فروشگاه شما پیدا نشد."
PROMPT = "🔎 نام، یوزرنیم یا آیدی تلگرام مشتری، یا لینک اشتراک را بفرستید. اگر لینک بفرستید، پیام شما پس از جستجو پاک می‌شود."
USER_NOT_FOUND_TEXT = "مشتری‌ای با این نام، یوزرنیم یا آیدی پیدا نشد."
SHORT_QUERY_TEXT = "حداقل دو نویسه از نام یا یوزرنیم را بفرستید."
MORE_RESULTS_NOTE = "\n<i>نتایج بیشتری هست؛ جستجو را دقیق‌تر کنید.</i>"
STATUS_LABELS = {
    "active": "فعال",
    "disabled": "غیرفعال",
    "expired": "منقضی",
    "limited": "حجم تمام‌شده",
    "on_hold": "در انتظار اتصال",
}


class LookupStates(StatesGroup):
    link = State()


def _plain(user: BotUser) -> str:
    return html.escape(user.full_name or user.username or str(user.telegram_id))


def _status(match: SubscriptionMatch) -> str:
    service = match.service
    if service.is_cancelled:
        return "لغو شده"
    return STATUS_LABELS.get(service.quota_status or "", "نامشخص")


def _quota_lines(match: SubscriptionMatch) -> list[str]:
    service = match.service
    if service.quota_synced_at is None:
        return []
    limit = int(service.quota_data_limit_bytes or 0)
    return [
        f"انقضا: {format_expire_short(service.quota_expire_at)}",
        f"حجم: {format_bytes(limit) if limit > 0 else 'نامحدود'}",
    ]


async def build_result(
    session: AsyncSession, match: SubscriptionMatch, *, shop_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Owner and service summary from stored data only (no panel call) with the action buttons."""
    owner, service = match.owner, match.service
    balance = await get_wallet_balance(session, owner, shop_id=shop_id)
    lines = [
        "🔎 <b>صاحب اشتراک</b>",
        f"مشتری: <b>{_plain(owner)}</b>" + (f" (@{html.escape(owner.username)})" if owner.username else ""),
        f"آیدی تلگرام: <code>{owner.telegram_id}</code>",
        f"کیف پول: {format_toman(balance, get_settings().currency)}",
        f"مسدود: {'بله 🚫' if owner.is_blocked else 'خیر'}",
        "",
        f"سرویس: <code>{html.escape(service.pg_username)}</code> (#{service.id})",
        f"پلن: {html.escape(match.plan_name) if match.plan_name else '—'}",
        f"وضعیت: {_status(match)}",
        *_quota_lines(match),
    ]
    if shop_id is None:
        # The platform cards refuse a customer attributed to a shop, so offer them only when they work.
        rows = [[InlineKeyboardButton(text="👤 پروفایل مشتری", callback_data=f"adm:users:view:{owner.id}")]]
        if owner.reseller_id is None:
            rows.append([InlineKeyboardButton(text="⚙️ مدیریت سرویس", callback_data=f"adm:users:svc:{owner.id}:{service.id}")])
        again, back = PLATFORM_ASK, PLATFORM_BACK
    else:
        rows = [[InlineKeyboardButton(text="👤 پروفایل مشتری", callback_data=f"res:user:{owner.id}")]]
        again, back = ASK_CALLBACK, SHOP_BACK
    rows.append([InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)])
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    return "\n".join(lines), markup


async def staff_lookup(
    session: AsyncSession, raw: str | None, *, shop_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """The result card for ``raw``, or the not-found text with a way back."""
    match = await find_service_by_link(session, raw, shop_id=shop_id)
    if match is not None:
        return await build_result(session, match, shop_id=shop_id)
    back = PLATFORM_BACK if shop_id is None else SHOP_BACK
    again = PLATFORM_ASK if shop_id is None else ASK_CALLBACK
    rows = [[InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)]]
    return NOT_FOUND_TEXT, with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)


def _user_label(user: BotUser) -> str:
    name = user.full_name or ""
    handle = f"@{user.username}" if user.username else ""
    return " · ".join(part for part in (name, handle, str(user.telegram_id)) if part)[:64]


async def staff_user_search(
    session: AsyncSession, raw: str | None, *, shop_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Customers of the shop whose name, @username or id match; each result opens the existing card."""
    scope = BotUser.reseller_id.is_(None) if shop_id is None else scoped_users_where(shop_id)
    users, more = await search_bot_users(session, raw, scope=scope)
    again, back = (PLATFORM_ASK, PLATFORM_BACK) if shop_id is None else (ASK_CALLBACK, SHOP_BACK)
    card = "adm:users:view:{}" if shop_id is None else "res:user:{}"
    rows = [[InlineKeyboardButton(text=_user_label(user), callback_data=card.format(user.id))] for user in users]
    rows.append([InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)])
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    if users:
        return "🔎 <b>نتیجهٔ جستجو</b>" + (MORE_RESULTS_NOTE if more else ""), markup
    short = len(clean_handle(raw)) < MIN_QUERY_LENGTH and not clean_handle(raw).isdigit()
    return (SHORT_QUERY_TEXT if short else USER_NOT_FOUND_TEXT), markup


async def staff_search(
    session: AsyncSession, raw: str | None, *, shop_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """A subscription link finds its owner; anything else searches the shop's customers."""
    if link_token(raw):
        return await staff_lookup(session, raw, shop_id=shop_id)
    return await staff_user_search(session, raw, shop_id=shop_id)


async def delete_typed_link(message: Message) -> None:
    """The link is a bearer secret; remove the staff member's message once it has been used."""
    try:
        await message.delete()
    except TelegramBadRequest:
        logger.debug("could not delete the typed subscription link", exc_info=True)


async def shop_scope(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> int | None:
    """The shop id of an operator holding ``dashboard``; None when not allowed."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile and has_bot_perm(profile, PERMISSION):
        return int(owner_id)
    return None


@router.callback_query(F.data == ASK_CALLBACK)
async def ask_link(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    if await shop_scope(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await ask_text(
        callback, state, prompt=PROMPT, cancel_code=CANCEL_CODE, fsm_state=LookupStates.link,
        return_to=SHOP_BACK, edit=True,
    )


@router.message(LookupStates.link)
async def receive_link(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text) and await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    shop_id = await shop_scope(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is None:
        await state.clear()
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    text, markup = await staff_search(session, message.text, shop_id=shop_id)
    await state.set_state(None)
    await finish_text_step(message, state, text=text, inline=markup)
    if link_token(message.text):
        await delete_typed_link(message)


async def _reopen_after_cancel(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_after_cancel))
