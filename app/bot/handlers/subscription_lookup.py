"""Staff search by subscription link, name, @username or id: buttons into the existing cards.

The platform Owner reaches it from the users search (see ``adm_users_search``); a shop operator
with ``dashboard`` from the customers list. Both are scoped to the shop that sold the service, and
a link of another shop answers exactly like an unknown one. The typed link is deleted afterwards.
"""
from __future__ import annotations

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
from app.db.models import BotUser, Role
from app.services import bot_feedback_texts as feedback
from app.bot.handlers.reseller_customer_services import (
    manage_service_button,
    may_manage_customer_services,
)
from app.services.formatting import format_bytes, format_expire_short, format_toman, kv_line
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.staff_cards import format_subscription_owner_card
from app.services.subscription_lookup import SubscriptionMatch, find_service_by_link, link_token
from app.services.user_search import MIN_QUERY_LENGTH, clean_handle, search_bot_users, service_owner_clause
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
SEARCH_PROMPT = (
    "🔎 <b>جستجوی کاربر</b>\n"
    "━━━━━━━━━━━━\n"
    "یکی از این‌ها را بفرستید:\n\n"
    "• نام یا بخشی از نام\n"
    "• یوزرنیم تلگرام، مثلاً <code>@username</code>\n"
    "• آیدی عددی تلگرام، مثلاً <code>123456789</code>\n"
    "  (یا حداقل ۴ رقم آخر آیدی)\n"
    "• نام‌کاربری سرویس (حداقل ۳ حرف)\n"
    "• لینک اشتراک یا توکن آن\n"
    "  <i>(پیام شما پس از جستجو پاک می‌شود)</i>"
)
USER_NOT_FOUND_TEXT = "مشتری‌ای با این نام، یوزرنیم یا آیدی پیدا نشد."
RESELLER_NOT_FOUND_TEXT = "نماینده‌ای با این نام، یوزرنیم یا آیدی پیدا نشد."
RESELLER_ASK = "adm:resellers:search"
RESELLER_BACK = "nv:adm:resellers"
RESELLER_SEARCH_PROMPT = (
    "🔎 <b>جستجوی نماینده</b>\n"
    "━━━━━━━━━━━━\n"
    "یکی از این‌ها را بفرستید:\n\n"
    "• نام یا بخشی از نام\n"
    "• یوزرنیم تلگرام، مثلاً <code>@username</code>\n"
    "• آیدی عددی تلگرام، مثلاً <code>123456789</code>"
)
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
    session: AsyncSession,
    match: SubscriptionMatch,
    *,
    shop_id: int | None,
    may_manage_services: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Owner and service summary from stored data only (no panel call) with the action buttons."""
    owner, service = match.owner, match.service
    balance = await get_wallet_balance(session, owner, shop_id=shop_id)
    quota = []
    for line in _quota_lines(match):
        if line.startswith("انقضا:"):
            quota.append(kv_line("📅", "انقضا", line.split(":", 1)[1].strip()))
        elif line.startswith("حجم:"):
            quota.append(kv_line("📦", "حجم", line.split(":", 1)[1].strip()))
        else:
            quota.append(line)
    text = format_subscription_owner_card(
        display_name=owner.full_name or owner.username or str(owner.telegram_id),
        username=owner.username,
        telegram_id=int(owner.telegram_id),
        wallet_text=format_toman(balance, get_settings().currency),
        is_blocked=bool(owner.is_blocked),
        service_username=service.pg_username,
        service_id=int(service.id),
        plan_name=match.plan_name,
        status_text=_status(match),
        quota_lines=quota,
    )
    if shop_id is None:
        # The platform cards refuse a customer attributed to a shop, so offer them only when they work.
        rows = [[InlineKeyboardButton(text="👤 پروفایل مشتری", callback_data=f"adm:users:view:{owner.id}")]]
        if owner.reseller_id is None:
            rows.append([InlineKeyboardButton(text="⚙️ مدیریت سرویس", callback_data=f"adm:users:svc:{owner.id}:{service.id}")])
        again, back = PLATFORM_ASK, PLATFORM_BACK
    else:
        rows = [[InlineKeyboardButton(text="👤 پروفایل مشتری", callback_data=f"res:user:{owner.id}")]]
        if may_manage_services:
            rows.append([manage_service_button(int(owner.id), int(service.id))])
        again, back = ASK_CALLBACK, SHOP_BACK
    rows.append([InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)])
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    return text, markup


async def staff_lookup(
    session: AsyncSession,
    raw: str | None,
    *,
    shop_id: int | None,
    may_manage_services: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """The result card for ``raw``, or the not-found text with a way back."""
    match = await find_service_by_link(session, raw, shop_id=shop_id)
    if match is not None:
        return await build_result(
            session, match, shop_id=shop_id, may_manage_services=may_manage_services
        )
    back = PLATFORM_BACK if shop_id is None else SHOP_BACK
    again = PLATFORM_ASK if shop_id is None else ASK_CALLBACK
    rows = [[InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)]]
    return NOT_FOUND_TEXT, with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)


def _user_label(user: BotUser) -> str:
    name = user.full_name or ""
    handle = f"@{user.username}" if user.username else ""
    return " · ".join(part for part in (name, handle, str(user.telegram_id)) if part)[:64]


async def _search_results(
    session: AsyncSession, raw: str | None, *, scope, card: str, again: str, back: str, not_found: str, also=None
) -> tuple[str, InlineKeyboardMarkup]:
    """Up to ten matches of ``raw`` in ``scope`` as buttons into ``card``, with search-again and back rows."""
    users, more = await search_bot_users(session, raw, scope=scope, also=also)
    rows = [[InlineKeyboardButton(text=_user_label(user), callback_data=card.format(user.id))] for user in users]
    rows.append([InlineKeyboardButton(text="🔎 جستجوی دوباره", callback_data=again)])
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    if users:
        return "🔎 <b>نتیجهٔ جستجو</b>" + (MORE_RESULTS_NOTE if more else ""), markup
    short = len(clean_handle(raw)) < MIN_QUERY_LENGTH and not clean_handle(raw).isdigit()
    return (SHORT_QUERY_TEXT if short else not_found), markup


async def staff_user_search(
    session: AsyncSession, raw: str | None, *, shop_id: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Customers of the shop whose name, @username or id match; each result opens the existing card."""
    scope = BotUser.reseller_id.is_(None) if shop_id is None else scoped_users_where(shop_id)
    again, back = (PLATFORM_ASK, PLATFORM_BACK) if shop_id is None else (ASK_CALLBACK, SHOP_BACK)
    card = "adm:users:view:{}" if shop_id is None else "res:user:{}"
    return await _search_results(
        session, raw, scope=scope, card=card, again=again, back=back, not_found=USER_NOT_FOUND_TEXT,
        also=service_owner_clause(raw, shop_id=shop_id),
    )


async def staff_reseller_search(session: AsyncSession, raw: str | None) -> tuple[str, InlineKeyboardMarkup]:
    """Resellers whose name, @username or id match, for the platform Owner; each opens the reseller card."""
    return await _search_results(
        session, raw, scope=BotUser.role == Role.RESELLER.value, card="adm:resellers:view:{}",
        again=RESELLER_ASK, back=RESELLER_BACK, not_found=RESELLER_NOT_FOUND_TEXT,
    )


async def staff_search(
    session: AsyncSession,
    raw: str | None,
    *,
    shop_id: int | None,
    may_manage_services: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """A subscription link finds its owner; anything else searches the shop's customers."""
    if link_token(raw):
        return await staff_lookup(
            session, raw, shop_id=shop_id, may_manage_services=may_manage_services
        )
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
    owner_id, _profile, _may = await shop_staff_context(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return owner_id


async def shop_staff_context(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> tuple[int | None, object | None, bool]:
    """Shop id for search (``dashboard``), plus whether service manage is allowed."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile and has_bot_perm(profile, PERMISSION):
        return int(owner_id), profile, may_manage_customer_services(profile)
    return None, None, False


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
        callback, state, prompt=SEARCH_PROMPT, cancel_code=CANCEL_CODE, fsm_state=LookupStates.link,
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
    shop_id, _profile, may_manage = await shop_staff_context(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await state.clear()
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    text, markup = await staff_search(
        session, message.text, shop_id=shop_id, may_manage_services=may_manage
    )
    await state.set_state(None)
    await finish_text_step(message, state, text=text, inline=markup)
    if link_token(message.text):
        await delete_typed_link(message)


async def _reopen_after_cancel(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_after_cancel))
