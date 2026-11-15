"""Owner credit-line controls on a reseller card, and credit summary on PAYG billing.

Limit entry uses the same ``ask_text`` / cancel / ``return_to`` path as wallet adjust
on the reseller card. Open and freeze stay edit-in-place subpanels.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import require_bot_owner_handler, require_platform_rep_mgmt
from app.bot.keyboards import back_button
from app.bot.nav_input import ask_text, finish_text_step, try_legacy_cancel
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, Role
from app.services import bot_feedback_texts as feedback
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.numbers import parse_int
from app.services.redact import user_safe_error
from app.services.shop_credit import (
    CreditLimitChange,
    CreditStatus,
    credit_account,
    credit_overview,
    set_credit_frozen,
    set_credit_limit,
)

logger = logging.getLogger(__name__)
router = Router(name="shop_credit_bot")

CREDIT_OPEN = re.compile(r"^adm:resellers:credit:(\d{1,9})\Z")
CREDIT_FREEZE = re.compile(r"^adm:resellers:credit:freeze:(\d{1,9}):([01])\Z")
CREDIT_LIMIT_ASK = re.compile(r"^adm:resellers:credit:limit:(\d{1,9})\Z")
CREDIT_TEMP_ASK = re.compile(r"^adm:resellers:credit:temp:(\d{1,9})\Z")
# Same ceiling as the web reseller credit form.
TEMP_LIMIT_MAX_DAYS = 365
STATUS_LABELS = {
    CreditStatus.ACTIVE: "فعال",
    CreditStatus.FROZEN: "مسدود موقت",
    CreditStatus.OVERDUE: "معوق",
    CreditStatus.BLOCKED: "مسدود",
}
HELP_LINE = "تغییر سقف، سقف موقت و مسدود موقت از همین‌جا؛ صورتحساب و بخشش از وب‌پنل."


class ShopCreditStates(StatesGroup):
    waiting_limit = State()
    waiting_temp_amount = State()
    waiting_temp_days = State()


def _is_admin(user: BotUser) -> bool:
    return user.role == Role.ADMIN.value


def credit_summary_lines(overview, *, currency: str, with_help: bool = False) -> list[str]:
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
    if with_help:
        lines.extend(["", HELP_LINE])
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
                text="⏳ سقف موقت",
                callback_data=f"adm:resellers:credit:temp:{user_id}",
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


def _active_temp_limit(account) -> tuple[int | None, datetime | None]:
    """Keep an unexpired temporary ceiling when only the permanent limit changes."""
    if account is None or account.temp_limit is None:
        return None, None
    until = account.temp_limit_until
    if until is None:
        return None, None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= datetime.now(timezone.utc):
        return None, None
    return int(account.temp_limit), until


def permanent_limit_change(account, amount: int) -> CreditLimitChange:
    """Update the permanent ceiling without clearing an active temporary one."""
    temp_limit, temp_until = _active_temp_limit(account)
    return CreditLimitChange(
        limit=int(amount),
        temp_limit=temp_limit,
        temp_limit_until=temp_until,
    )


async def _load_reseller(session: AsyncSession, user_id: int) -> BotUser | None:
    user = await session.get(BotUser, int(user_id))
    if user is None or user.role != Role.RESELLER.value:
        return None
    return user


async def _wallet_balance(session: AsyncSession, user: BotUser) -> int:
    """Same PAYG wallet source as reseller billing when the shop is PAYG."""
    from app.services.billing import ensure_payg_shop_wallet, is_payg
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, int(user.id))
    if profile is not None and is_payg(profile):
        _owner, bal = await ensure_payg_shop_wallet(session, profile)
        await session.commit()
        return int(bal)
    return int(user.wallet_balance or 0)


async def _render_credit_panel(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    preface: str = "",
) -> None:
    balance = await _wallet_balance(session, user)
    overview = await credit_overview(session, int(user.id), balance=balance)
    currency = get_settings().currency
    lines = credit_summary_lines(overview, currency=currency, with_help=True)
    if preface:
        lines = [preface, ""] + lines
    frozen = overview.status == CreditStatus.FROZEN
    await safe_edit_text(
        message,
        "\n".join(lines),
        reply_markup=_credit_keyboard(int(user.id), frozen=frozen),
    )


@router.callback_query(F.data.regexp(CREDIT_OPEN.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def open_credit_panel(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
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
    await state.set_state(None)
    await callback.answer()
    if callback.message is not None:
        await _render_credit_panel(callback.message, session, user)


@router.callback_query(F.data.regexp(CREDIT_FREEZE.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def toggle_credit_freeze(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
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
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await state.set_state(None)
    await callback.answer("انجام شد")
    if callback.message is not None:
        await _render_credit_panel(callback.message, session, user)


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
    await state.update_data(shop_credit_user_id=int(user.id))
    await callback.answer()
    await ask_text(
        callback,
        state,
        prompt=(
            f"💳 سقف اعتبار برای نماینده #{user.id}\n\n"
            "مبلغ را به تومان بفرستید (۰ برای حذف سقف)."
        ),
        cancel_code="adm_res",
        return_to=f"adm:resellers:credit:{user.id}",
        fsm_state=ShopCreditStates.waiting_limit,
        edit=True,
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
        if await try_legacy_cancel(message, state, session, db_user):
            return
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
    account = await credit_account(session, int(user.id))
    try:
        await set_credit_limit(
            session,
            int(user.id),
            permanent_limit_change(account, amount),
            actor=f"tg:{db_user.telegram_id}",
        )
    except ValueError as exc:
        await rollback_quiet(session)
        await message.answer(f"⚠️ {user_safe_error(exc)}")
        return
    balance = await _wallet_balance(session, user)
    overview = await credit_overview(session, int(user.id), balance=balance)
    currency = get_settings().currency
    frozen = overview.status == CreditStatus.FROZEN
    lines = ["✅ سقف اعتبار ذخیره شد.", ""] + credit_summary_lines(
        overview, currency=currency, with_help=True
    )
    await finish_text_step(
        message,
        state,
        text="\n".join(lines),
        inline=_credit_keyboard(int(user.id), frozen=frozen),
    )


@router.callback_query(F.data.regexp(CREDIT_TEMP_ASK.pattern))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def ask_temp_credit_limit(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    match = CREDIT_TEMP_ASK.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    user = await _load_reseller(session, int(match.group(1)))
    if user is None:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    await state.update_data(shop_credit_user_id=int(user.id))
    await callback.answer()
    await ask_text(
        callback,
        state,
        prompt=(
            f"⏳ سقف موقت برای نماینده #{user.id}\n\n"
            "مبلغ را به تومان بفرستید (۰ برای حذف سقف موقت)."
        ),
        cancel_code="adm_res",
        return_to=f"adm:resellers:credit:{user.id}",
        fsm_state=ShopCreditStates.waiting_temp_amount,
        edit=True,
    )


@router.message(ShopCreditStates.waiting_temp_amount)
@require_bot_owner_handler
async def save_temp_credit_amount(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        if await try_legacy_cancel(message, state, session, db_user):
            return
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
        await message.answer("سقف موقت نمی‌تواند منفی باشد.")
        return
    if amount == 0:
        account = await credit_account(session, int(user.id))
        try:
            await set_credit_limit(
                session,
                int(user.id),
                CreditLimitChange(limit=int(account.credit_limit if account else 0)),
                actor=f"tg:{db_user.telegram_id}",
            )
        except ValueError as exc:
            await rollback_quiet(session)
            await message.answer(f"⚠️ {user_safe_error(exc)}")
            return
        balance = await _wallet_balance(session, user)
        overview = await credit_overview(session, int(user.id), balance=balance)
        currency = get_settings().currency
        frozen = overview.status == CreditStatus.FROZEN
        lines = ["✅ سقف موقت حذف شد.", ""] + credit_summary_lines(
            overview, currency=currency, with_help=True
        )
        await finish_text_step(
            message,
            state,
            text="\n".join(lines),
            inline=_credit_keyboard(int(user.id), frozen=frozen),
        )
        return
    await state.update_data(shop_credit_temp_amount=amount)
    await ask_text(
        message,
        state,
        prompt=f"مدت سقف موقت را به روز بفرستید (۱ تا {TEMP_LIMIT_MAX_DAYS}).",
        cancel_code="adm_res",
        return_to=f"adm:resellers:credit:{user.id}",
        fsm_state=ShopCreditStates.waiting_temp_days,
        edit=False,
    )


@router.message(ShopCreditStates.waiting_temp_days)
@require_bot_owner_handler
async def save_temp_credit_days(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    data = await state.get_data()
    user_id = int(data.get("shop_credit_user_id") or 0)
    amount = int(data.get("shop_credit_temp_amount") or 0)
    user = await _load_reseller(session, user_id) if user_id else None
    if user is None or amount <= 0:
        await state.clear()
        await message.answer("نماینده یافت نشد")
        return
    try:
        days = parse_int(str(message.text or "").strip())
    except ValueError:
        await message.answer(f"یک عدد بین ۱ و {TEMP_LIMIT_MAX_DAYS} بفرستید.")
        return
    if days < 1 or days > TEMP_LIMIT_MAX_DAYS:
        await message.answer(f"مدت سقف موقت باید بین ۱ تا {TEMP_LIMIT_MAX_DAYS} روز باشد.")
        return
    account = await credit_account(session, int(user.id))
    until = datetime.now(timezone.utc) + timedelta(days=days)
    try:
        await set_credit_limit(
            session,
            int(user.id),
            CreditLimitChange(
                limit=int(account.credit_limit if account else 0),
                temp_limit=amount,
                temp_limit_until=until,
            ),
            actor=f"tg:{db_user.telegram_id}",
        )
    except ValueError as exc:
        await rollback_quiet(session)
        await message.answer(f"⚠️ {user_safe_error(exc)}")
        return
    balance = await _wallet_balance(session, user)
    overview = await credit_overview(session, int(user.id), balance=balance)
    currency = get_settings().currency
    frozen = overview.status == CreditStatus.FROZEN
    lines = ["✅ سقف موقت ذخیره شد.", ""] + credit_summary_lines(
        overview, currency=currency, with_help=True
    )
    await finish_text_step(
        message,
        state,
        text="\n".join(lines),
        inline=_credit_keyboard(int(user.id), frozen=frozen),
    )
