"""Shop owner buying extra volume, users or a catalog pack in the bot: a confirm screen, then the purchase.

The list screens only choose what to buy. The confirm screen shows what is bought, the price and
the wallet balance; the pay button carries the subscription's generation, so a double tap or an
old message buys at most once. The money rules live in ``reseller_capacity`` and ``reseller_packs``.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_toman
from app.services.redact import user_safe_error
from app.services.reseller_access import load_reseller_capacity_actor
from app.services.reseller_capacity import (
    buy_extra_gb,
    buy_extra_users,
    load_reseller_plan,
)
from app.services.reseller_capacity_orders import (
    KIND_GB,
    KIND_PACK,
    KIND_USERS,
    NOT_FOR_SHOP_BOT,
    create_capacity_order,
    describe,
    parse_note,
)
from app.services.reseller_packs import buy_addon_pack
from app.services.reseller_plan_extra_packs import (
    active_amounts_for_plan,
    get_pack_for_plan,
)

logger = logging.getLogger(__name__)

router = Router(name="reseller_capacity_buy")

HUB = "res:addons"
ORDER = "res:cap:o:"
PACK_OK = "res:addon:ok:"
GB_OK = "res:buy_gb_ok:"
USERS_OK = "res:buy_users_ok:"
_ID_MAX = 2**31 - 1


def _money(amount: int) -> str:
    return format_toman(int(amount or 0), get_settings().currency)


def confirm_view(
    *,
    title: str,
    lines: list[str],
    price: int,
    wallet: int,
    ok_callback: str,
    back_callback: str,
    order_callback: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """The «are you sure» screen: what is bought, what it costs and what the wallet holds.

    ``order_callback`` adds «other payment methods» (an order paid like a plan); the platform bot only.
    """
    body = "\n".join(lines) + (
        f"\n\nمبلغ: <b>{_money(price)}</b>\nموجودی کیف پول: <b>{_money(wallet)}</b>\n"
        "با تأیید، مبلغ همین حالا از کیف پول کسر و ظرفیت اعمال می‌شود."
    )
    rows = [[InlineKeyboardButton(text=f"✅ پرداخت از کیف پول — {_money(price)}", callback_data=ok_callback)]]
    if order_callback:
        rows.append([InlineKeyboardButton(text="💳 سایر روش‌های پرداخت", callback_data=order_callback)])
    rows.append([kb.back_button(None, back_callback)])
    return format_message(title, body), InlineKeyboardMarkup(inline_keyboard=rows)


async def current_generation(session: AsyncSession, profile: ResellerProfile) -> int:
    """The subscription's renew generation the next purchase must carry (0 before the first row)."""
    from app.services.pg_admin_subscription import get_subscription

    sub = await get_subscription(session, (profile.pg_admin_username or "").strip())
    return int(sub.renew_generation or 0) if sub is not None else 0


async def _actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> tuple[BotUser, ResellerProfile, ResellerPlan | None] | None:
    owner_id, profile = await load_reseller_capacity_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or profile is None:
        return None
    owner = await session.get(BotUser, int(owner_id))
    if owner is None:
        return None
    return owner, profile, await load_reseller_plan(session, profile)


def _parse_ok(data: str, prefix: str) -> tuple[int, int] | None:
    """(item, generation) from ``prefix{item}:{generation}``; None for anything malformed.

    Also accepts ``prefixid:{pack_id}:{generation}`` where item is the pack id.
    """
    rest = data.removeprefix(prefix)
    parts = rest.split(":")
    if len(parts) == 3 and parts[0] == "id" and parts[1].isdigit() and parts[2].isdigit():
        item, generation = int(parts[1]), int(parts[2])
        return (item, generation) if 0 < item <= _ID_MAX and generation <= _ID_MAX else None
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    item, generation = int(parts[0]), int(parts[1])
    return (item, generation) if 0 < item <= _ID_MAX and generation <= _ID_MAX else None


def _ok_uses_pack_id(data: str, prefix: str) -> bool:
    return data.removeprefix(prefix).startswith("id:")


async def _show_done(callback: CallbackQuery, title: str, lines: list[str]) -> None:
    await callback.answer("خرید شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(title, "\n".join(lines)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, HUB)]]),
        )


@router.callback_query(F.data.startswith(PACK_OK))
async def res_addon_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    parsed = _parse_ok(callback.data or "", PACK_OK)
    actor = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    owner, profile, plan = actor
    try:
        result = await buy_addon_pack(
            session, user=owner, profile=profile, plan=plan, pack_id=parsed[0], generation=parsed[1],
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    lines = [f"<b>{html.escape(str(result['pack']))}</b>"]
    if result["gb"]:
        lines.append(f"حجم اضافه: {result['gb']} گیگابایت")
    if result["users"]:
        lines.append(f"کاربر اضافه: {result['users']}")
    lines.append(f"مبلغ: {_money(int(result['amount']))}")
    await _show_done(callback, "✅ بسته اعمال شد", [*lines, "تاریخ انقضا تغییر نکرد."])


@router.callback_query(F.data.startswith(GB_OK))
async def res_buy_gb_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    parsed = _parse_ok(callback.data or "", GB_OK)
    actor = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None or actor[2] is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    owner, profile, plan = actor
    if parsed is None:
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    gb = parsed[0]
    if _ok_uses_pack_id(callback.data or "", GB_OK):
        pack = await get_pack_for_plan(session, plan_id=int(plan.id), pack_id=parsed[0])
        if pack is None or pack.kind != KIND_GB or not pack.is_active:
            await callback.answer("مقدار مجاز نیست", show_alert=True)
            return
        gb = int(pack.amount)
    elif gb not in await active_amounts_for_plan(session, int(plan.id), KIND_GB):
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    try:
        result = await buy_extra_gb(
            session, user=owner, profile=profile, plan=plan, gb=gb, generation=parsed[1],
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await _show_done(
        callback, "✅ حجم اضافه خریداری شد",
        [f"مقدار: {result['gb']} گیگابایت", f"مبلغ: {_money(int(result['amount']))}"],
    )


@router.callback_query(F.data.startswith(USERS_OK))
async def res_buy_users_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    parsed = _parse_ok(callback.data or "", USERS_OK)
    actor = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None or actor[2] is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    owner, profile, plan = actor
    if parsed is None:
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    count = parsed[0]
    if _ok_uses_pack_id(callback.data or "", USERS_OK):
        pack = await get_pack_for_plan(session, plan_id=int(plan.id), pack_id=parsed[0])
        if pack is None or pack.kind != KIND_USERS or not pack.is_active:
            await callback.answer("مقدار مجاز نیست", show_alert=True)
            return
        count = int(pack.amount)
    elif count not in await active_amounts_for_plan(session, int(plan.id), KIND_USERS):
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    try:
        result = await buy_extra_users(
            session, user=owner, profile=profile, plan=plan, count=count, generation=parsed[1],
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await _show_done(
        callback, "✅ کاربر اضافه خریداری شد",
        [f"تعداد: {result['users']}", f"سقف جدید: {result['max_users']}", f"مبلغ: {_money(int(result['amount']))}"],
    )


def order_callback_for(kind: str, ref: int, *, is_reseller_bot: bool) -> str | None:
    """The «other payment methods» button; a shop bot's payment methods belong to the shop, so it gets none."""
    return None if is_reseller_bot else f"{ORDER}{kind}:{int(ref)}"


@router.callback_query(F.data.startswith(ORDER))
async def res_capacity_order(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Create the order for a confirmed purchase and show the payment methods, like a plan."""
    from app.bot.menu_nav import present_order_pay

    if is_reseller_bot:
        await callback.answer(NOT_FOR_SHOP_BOT, show_alert=True)
        return
    parts = (callback.data or "").removeprefix(ORDER).split(":")
    actor = await _actor(session, db_user, is_reseller_bot=False, reseller_owner_id=None)
    if actor is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if len(parts) != 2 or parts[0] not in {KIND_PACK, KIND_GB, KIND_USERS} or not parts[1].isdigit() or not 0 < int(parts[1]) <= _ID_MAX:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    owner, profile, plan = actor
    try:
        order = await create_capacity_order(
            session, user=owner, profile=profile, plan=plan, kind=parts[0], ref=int(parts[1]),
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await callback.answer()
    parsed = parse_note(order.note)
    summary = format_message(
        f"➕ ظرفیت نمایندگی — سفارش #{order.id}",
        f"افزایش: {describe(parsed) if parsed else '—'}\nمبلغ: <b>{_money(int(order.amount))}</b>",
    )
    if callback.message:
        await present_order_pay(
            callback.message, session, db_user, order.id, state=state, summary=summary,
            is_reseller_bot=False, reseller_owner_id=None,
        )
