"""Owner credit-line controls on a reseller card, and credit summary on PAYG billing."""

from __future__ import annotations

import html
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import require_bot_owner_handler, require_platform_rep_mgmt
from app.bot.keyboards import back_button
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, Role
from app.services import bot_feedback_texts as feedback
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.numbers import parse_int
from app.services.shop_credit import (
    CreditLimitChange,
    CreditStatus,
    credit_overview,
    set_credit_frozen,
    set_credit_limit,
)

logger = logging.getLogger(__name__)
router = Router(name="shop_credit_bot")

CREDIT_OPEN = re.compile(r"^adm:resellers:credit:(\d{1,9})\Z")
CREDIT_FREEZE = re.compile(r"^adm:resellers:credit:freeze:(\d{1,9}):([01])\Z")
CREDIT_LIMIT_ASK = re.compile(r"^adm:resellers:credit:limit:(\d{1,9})\Z")
STATUS_LABELS = {
    CreditStatus.ACTIVE: "فعال",
    CreditStatus.FROZEN: "مسدود موقت",
    CreditStatus.OVERDUE: "معوق",
    CreditStatus.BLOCKED: "مسدود",
}


class ShopCreditStates(StatesGroup):
    waiting_limit = State()


def _is_admin(user: BotUser) -> bool:
    return user.role == Role.ADMIN.value


def credit_summary_lines(overview, *, currency: str) -> list[str]:
    """Shared read-only lines for owner card and reseller billing."""
    usage = overview.usage
    status = STATUS_LABELS.get(overview.status, overview.status)
    lines = [
        "💳 <b>پلن اعتباری</b>",
        f"وضعیت: <b>{html.escape(str(status))}</b>",
        f"سقف: {format_toman(usage.limit, currency)}",
        f"مصرف‌شده: {format_toman(usage.used, currency)} ({usage.percent}٪)",
        f"آزاد: {format_toman(usage.available, currency)}",
        f"موجودی کیف: {format_toman(overview.balance, currency)}",
    ]
    account = overview.account
    if account is not None and account.temp_limit is not None:
        lines.append(f"سقف موقت: {format_toman(int(account.temp_limit), currency)}")
    return lines


def _credit_keyboard(user_id: int, *, frozen: bool) -> InlineKeyboardMarkup:
    freeze_label = "🔓 بازکردن اعتبار" if frozen else "🔒 مسدود موقت اعتبار"
    freeze_target = "0" if frozen else "1"
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ تنظیم سقف اعتبار",
                callback_data=f"adm:resellers:credit:limit:{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text=freeze_label,
                callback_data=f"adm:resellers:credit:freeze:{user_id}:{freeze_target}",
            )
        ],
        [back_button(None, f"adm:resellers:view:{user_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_reseller(session: AsyncSession, user_id: int) -> BotUser | None:
    user = await session.get(BotUser, int(user_id))
    if user is None or user.role != Role.RESELLER.value:
        return None
    return user


@router.callback_query(F.data.regexp(CREDIT_OPEN.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def open_credit_panel(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    match = CREDIT_OPEN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    user = await _load_reseller(session, int(match.group(1)))
    if user is None:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    await callback.answer()
    overview = await credit_overview(session, int(user.id), balance=int(user.wallet_balance or 0))
    currency = get_settings().currency
    lines = credit_summary_lines(overview, currency=currency)
    lines.extend(
        [
            "",
            "تغییر سقف و مسدود موقت از همین‌جا؛ صورتحساب و بخشش از وب‌پنل.",
        ]
    )
    frozen = overview.status == CreditStatus.FROZEN
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=_credit_keyboard(int(user.id), frozen=frozen),
        )


@router.callback_query(F.data.regexp(CREDIT_FREEZE.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def toggle_credit_freeze(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    match = CREDIT_FREEZE.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    user = await _load_reseller(session, int(match.group(1)))
    if user is None:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    try:
        await set_credit_frozen(
            session,
            int(user.id),
            match.group(2) == "1",
            actor=f"tg:{db_user.telegram_id}",
        )
    except ValueError as exc:
        await rollback_quiet(session)
        await callback.answer(str(exc) or "ناموفق", show_alert=True)
        return
    await callback.answer("انجام شد")
    overview = await credit_overview(session, int(user.id), balance=int(user.wallet_balance or 0))
    currency = get_settings().currency
    frozen = overview.status == CreditStatus.FROZEN
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            "\n".join(credit_summary_lines(overview, currency=currency)),
            reply_markup=_credit_keyboard(int(user.id), frozen=frozen),
        )


@router.callback_query(F.data.regexp(CREDIT_LIMIT_ASK.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def ask_credit_limit(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    match = CREDIT_LIMIT_ASK.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    user = await _load_reseller(session, int(match.group(1)))
    if user is None:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    await state.set_state(ShopCreditStates.waiting_limit)
    await state.update_data(shop_credit_user_id=int(user.id))
    await callback.answer()
    if callback.message is not None:
        await safe_edit_text(
            callback.message,
            f"سقف اعتبار نماینده #{user.id} را به تومان بفرستید (۰ برای حذف سقف).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[back_button(None, f"adm:resellers:credit:{user.id}")]]
            ),
        )


@router.message(ShopCreditStates.waiting_limit)
@require_bot_owner_handler
async def save_credit_limit(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    data = await state.get_data()
    user_id = int(data.get("shop_credit_user_id") or 0)
    user = await _load_reseller(session, user_id) if user_id else None
    if user is None:
        await state.clear()
        await message.answer("نماینده یافت نشد")
        return
    try:
        amount = parse_int(str(message.text or "").strip())
    except ValueError:
        await message.answer("یک عدد صحیح نامنفی بفرستید.")
        return
    if amount < 0:
        await message.answer("سقف اعتبار نمی‌تواند منفی باشد.")
        return
    try:
        await set_credit_limit(
            session,
            int(user.id),
            CreditLimitChange(limit=amount),
            actor=f"tg:{db_user.telegram_id}",
        )
    except ValueError as exc:
        await rollback_quiet(session)
        await message.answer(str(exc) or "ذخیره نشد")
        return
    await state.clear()
    overview = await credit_overview(session, int(user.id), balance=int(user.wallet_balance or 0))
    currency = get_settings().currency
    frozen = overview.status == CreditStatus.FROZEN
    await message.answer(
        "\n".join(credit_summary_lines(overview, currency=currency)),
        reply_markup=_credit_keyboard(int(user.id), frozen=frozen),
    )
