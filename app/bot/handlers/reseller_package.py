"""Shop owner renewing their own package in the bot, or moving to a larger or smaller one.

Thin handlers over ``app.services.reseller_package``. Every pay button carries the
subscription's renew generation, so a double tap or an old message renews at most once.
On the platform bot, «other payment methods» creates a frozen order settled like a plan.
Screens are edited in place and each has a back button to the previous one.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_toman
from app.services.redact import user_safe_error
from app.services.pg_admin_subscription import plan_has_time
from app.services.reseller_access import load_reseller_capacity_actor
from app.services.reseller_package import (
    MOVE_UNTIMED_EXPLAIN,
    RELATION_CURRENT,
    RELATION_DOWNGRADE,
    RENEW_EXPLAIN,
    RELATION_UPGRADE,
    STALE_PAGE,
    WALLET_PAY_HINT,
    PackageChoice,
    PackageOverview,
    package_overview,
    renew_package,
    renewal_effect,
)
from app.services.reseller_package_orders import NOT_FOR_SHOP_BOT, create_package_order

logger = logging.getLogger(__name__)

router = Router(name="reseller_package")

LIST = "res:pkg"
VIEW = "res:pkg:v:"
PAY = "res:pkg:ok:"
ORDER = "res:pkg:o:"
RENEW_SCREEN = "res:renew"
RELATION_ICONS = {RELATION_CURRENT: "🔄", RELATION_UPGRADE: "⬆️", RELATION_DOWNGRADE: "⬇️"}
_ID_MAX = 2**31 - 1


async def _owner(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> tuple[BotUser, ResellerProfile] | None:
    owner_id, profile = await load_reseller_capacity_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or profile is None:
        return None
    owner = await session.get(BotUser, int(owner_id))
    return (owner, profile) if owner is not None else None


def _money(amount: int) -> str:
    return format_toman(int(amount or 0), get_settings().currency)


def _capacity(choice: PackageChoice) -> str:
    plan = choice.plan
    users = f"{int(plan.included_users)} کاربر" if plan.included_users else "کاربر نامحدود"
    gb = f"{int(plan.included_gb)} گیگ" if plan.included_gb else "حجم نامحدود"
    days = f"{int(plan.duration_days)} روز" if plan.duration_days else "بدون انقضا"
    return f"{users} · {gb} · {days}"


def _expiry_line(label: str, expires: datetime | None) -> str:
    if not isinstance(expires, datetime):
        return ""
    return f"\n{label}: <b>{expires.strftime('%Y-%m-%d %H:%M')}</b> UTC"


def _parse_ids(data: str, prefix: str) -> tuple[int, int] | None:
    """(plan_id, generation) from ``prefix{plan}:{gen}``; None for anything malformed."""
    parts = data.removeprefix(prefix).split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    plan_id, generation = int(parts[0]), int(parts[1])
    if not 0 < plan_id <= _ID_MAX or generation > _ID_MAX:
        return None
    return plan_id, generation


def renew_button(price: int, plan_id: int, generation: int) -> InlineKeyboardButton:
    """Wallet pay on the renew screen: charges the current package once for this generation."""
    return InlineKeyboardButton(
        text=f"✅ پرداخت از کیف پول — {_money(price)}",
        callback_data=f"{PAY}{plan_id}:{generation}",
    )


def change_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🔁 ارتقا یا تغییر بسته", callback_data=LIST)


def order_callback_for(plan_id: int, generation: int, *, is_reseller_bot: bool) -> str | None:
    """«Other payment methods»; a shop bot's methods belong to the shop, so it gets none."""
    return None if is_reseller_bot else f"{ORDER}{int(plan_id)}:{int(generation)}"


def _list_view(overview: PackageOverview) -> tuple[str, InlineKeyboardMarkup]:
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for choice in overview.choices:
        if choice.relation == RELATION_CURRENT:
            continue
        name = html.escape(str(choice.plan.name or ""))
        line = f"• <b>{name}</b> ({choice.label}) — {_capacity(choice)} — {_money(choice.invoice.get('total', 0))}"
        if choice.blocked:
            lines.append(f"{line}\n   ⛔️ {html.escape(choice.blocked)}")
            continue
        lines.append(line)
        icon = RELATION_ICONS.get(choice.relation, "🔁")
        rows.append([InlineKeyboardButton(
            text=f"{icon} {choice.label}: {choice.plan.name}",
            callback_data=f"{VIEW}{int(choice.plan.id)}:{overview.generation}",
        )])
    rows.append([kb.back_button(None, RENEW_SCREEN)])
    current = html.escape(str(overview.current.name)) if overview.current else "—"
    body = f"بستهٔ فعلی: <b>{current}</b>\n\n" + ("\n".join(lines) if lines else "بستهٔ دیگری برای تغییر نیست.")
    if overview.usage is None:
        body += "\n\n⚠️ مصرف فعلی از پنل خوانده نشد؛ بسته‌های کوچک‌تر هنگام پرداخت دوباره بررسی می‌شوند."
    return format_message("🔁 ارتقا یا تغییر بسته", body), InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_view(
    choice: PackageChoice,
    generation: int,
    *,
    order_callback: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    from app.services.reseller_package import renew_confirm_warnings_from_invoice

    total = int(choice.invoice.get("total", 0) or 0)
    warn_lines = renew_confirm_warnings_from_invoice(choice.plan, choice.invoice)
    warn_block = "".join(f"\n⚠️ {html.escape(w)}" for w in warn_lines)
    body = (
        f"بستهٔ جدید: <b>{html.escape(str(choice.plan.name or ''))}</b> ({choice.label})\n"
        f"ظرفیت: {_capacity(choice)}\n"
        f"مبلغ: <b>{_money(total)}</b>\n\n"
        + (RENEW_EXPLAIN if plan_has_time(choice.plan) else MOVE_UNTIMED_EXPLAIN)
        + warn_block
        + f"\n{WALLET_PAY_HINT}"
    )
    rows = [
        [InlineKeyboardButton(
            text=f"✅ پرداخت از کیف پول — {_money(total)}",
            callback_data=f"{PAY}{int(choice.plan.id)}:{generation}",
        )],
    ]
    if order_callback:
        rows.append([InlineKeyboardButton(text="💳 سایر روش‌های پرداخت", callback_data=order_callback)])
    rows.append([kb.back_button(None, LIST)])
    return format_message(f"{RELATION_ICONS.get(choice.relation, '🔁')} {choice.label} بسته", body), InlineKeyboardMarkup(
        inline_keyboard=rows,
    )


def _done_text(result: dict) -> str:
    moved = f"بستهٔ جدید: <b>{html.escape(str(result.get('plan_name') or ''))}</b>\n" if result.get("changed") else ""
    effect = renewal_effect(result)
    warnings = "".join(f"\n⚠️ {html.escape(str(w))}" for w in result.get("warnings") or [])
    return format_message(
        "✅ تمدید انجام شد",
        f"{moved}مبلغ: {_money(result.get('amount', 0))}\n{effect}"
        + _expiry_line("انقضای جدید", result.get("expires_at"))
        + warnings,
    )


async def _show_list(callback: CallbackQuery, session: AsyncSession, profile: ResellerProfile) -> None:
    try:
        overview = await package_overview(session, profile)
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await callback.answer()
    if callback.message:
        text, markup = _list_view(overview)
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data == LIST)
async def res_package_list(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _owner(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    await _show_list(callback, session, actor[1])


@router.callback_query(F.data.startswith(VIEW))
async def res_package_view(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _owner(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    ids = _parse_ids(callback.data or "", VIEW)
    if actor is None or ids is None:
        await callback.answer(feedback.RESELLERS_ONLY if actor is None else "درخواست نامعتبر است", show_alert=True)
        return
    try:
        overview = await package_overview(session, actor[1])
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    plan_id, generation = ids
    choice = next((c for c in overview.choices if int(c.plan.id) == plan_id), None)
    if generation != overview.generation or choice is None or choice.relation == RELATION_CURRENT:
        await callback.answer(STALE_PAGE[:180], show_alert=True)
        return
    if choice.blocked:
        await callback.answer(choice.blocked[:180], show_alert=True)
        return
    await callback.answer()
    if callback.message:
        text, markup = _confirm_view(
            choice,
            overview.generation,
            order_callback=order_callback_for(
                int(choice.plan.id), overview.generation, is_reseller_bot=is_reseller_bot,
            ),
        )
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith(PAY))
async def res_package_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _owner(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    ids = _parse_ids(callback.data or "", PAY)
    if actor is None or ids is None:
        await callback.answer(feedback.RESELLERS_ONLY if actor is None else "درخواست نامعتبر است", show_alert=True)
        return
    owner, profile = actor
    plan_id, generation = ids
    try:
        result = await renew_package(session, owner=owner, profile=profile, plan_id=plan_id, generation=generation)
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await callback.answer(feedback.RENEWED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            _done_text(result),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, back_callback_for("res_renew"))]]),
        )


@router.callback_query(F.data.startswith(ORDER))
async def res_package_order(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Create the renew/upgrade order and show platform payment methods."""
    from app.bot.menu_nav import present_order_pay

    if is_reseller_bot:
        await callback.answer(NOT_FOR_SHOP_BOT, show_alert=True)
        return
    actor = await _owner(session, db_user, is_reseller_bot=False, reseller_owner_id=None)
    ids = _parse_ids(callback.data or "", ORDER)
    if actor is None:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if ids is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    owner, profile = actor
    plan_id, generation = ids
    try:
        order = await create_package_order(
            session, owner=owner, profile=profile, plan_id=plan_id, generation=generation,
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=180), show_alert=True)
        return
    await callback.answer()
    target = await session.get(ResellerPlan, plan_id)
    plan_label = html.escape(str(target.name if target is not None else plan_id))
    summary = format_message(
        f"🔄 تمدید بسته نمایندگی — سفارش #{order.id}",
        f"بسته: <b>{plan_label}</b>\nمبلغ: <b>{_money(int(order.amount))}</b>",
    )
    if callback.message:
        await present_order_pay(
            callback.message,
            session,
            db_user,
            order.id,
            state=state,
            summary=summary,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
