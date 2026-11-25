"""Bot: turn a customer or a shop off with all its services, or back on.

The same service as the web panel does the work, with the same checks. Who acts comes from the
bot the update arrived on: on a shop's bot, that shop's staff with ``accounts_disable``; on the
platform bot, only the owner principal. The customer or shop id in the callback is re-checked by
the service against the acting scope on every step.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_bot_owner_principal
from app.bot.handlers.reseller_customer_tools import shop_bot_staff
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.account_hold_actions import (
    HoldPanel,
    customer_hold_panel,
    disable_customer,
    disable_shop,
    enable_customer,
    enable_shop,
    shop_hold_panel,
)
from app.services.db_safe import rollback_quiet
from app.services.org_principals import get_active_owner
from app.services.reseller_access import load_reseller_actor

logger = logging.getLogger(__name__)
router = Router(name="account_hold_bot")

PREFIX = kb.ACCOUNT_HOLD_PREFIX
KIND_CUSTOMER = "c"
KIND_SHOP = "s"
STATUS_TEXT = {
    "applying": "⏳ در حال خاموش کردن سرویس‌ها",
    "held": "⛔ غیرفعال",
    "releasing": "⏳ در حال فعال‌سازی دوباره",
}


FAILED = "انجام نشد؛ کمی بعد دوباره تلاش کنید"


class AccountHoldStates(StatesGroup):
    reason = State()


async def _owner_staff(session: AsyncSession, db_user: BotUser) -> dict[str, Any] | None:
    owner = await get_active_owner(session)
    if owner is None:
        return None
    handle = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    return {
        "role": "admin", "id": int(db_user.id), "bot_user_id": int(db_user.id), "username": f"ربات: {handle}"[:64],
        "org_principal_id": int(owner.id), "org_depth": 0, "org_parent_id": None, "org_status": "active",
    }


async def _actor(session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool,
                 reseller_owner_id: int | None) -> dict[str, Any] | None:
    """Staff dict of the person acting on this bot, or None when they may not act here.

    The owner principal acts on the platform bot only; otherwise the shop the person works for.
    """
    if not is_reseller_bot and await is_bot_owner_principal(session, db_user, is_reseller_bot=False):
        return await _owner_staff(session, db_user)
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return shop_bot_staff(profile, db_user) if owner_id and profile else None


def _parse(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[1] not in (KIND_CUSTOMER, KIND_SHOP) or not parts[2].isdigit() or len(parts[2]) > 12:
        return None
    return parts[1], int(parts[2])


def _back(kind: str, target: int, staff: dict[str, Any] | None) -> InlineKeyboardButton:
    """Back to the card this screen was opened from: the owner's cards, or the shop's customer card."""
    if kind == KIND_SHOP:
        return kb.back_button(None, f"adm:resellers:view:{target}")
    owner = bool(staff) and staff.get("org_depth") == 0
    return kb.back_button(None, f"adm:users:view:{target}" if owner else f"res:user:{target}")


async def _panel(session: AsyncSession, staff: dict[str, Any], kind: str, target: int) -> HoldPanel | None:
    if kind == KIND_SHOP:
        return await shop_hold_panel(session, staff, target)
    return await customer_hold_panel(session, staff, target)


def _panel_text(kind: str, panel: HoldPanel) -> str:
    noun = "این نماینده، زیرنماینده‌هایش، ادمین پاسارگارد و همهٔ کاربرانش" if kind == KIND_SHOP else "این حساب و همهٔ سرویس‌هایش"
    hold = panel.hold
    if hold is None:
        return f"{noun} فعال است.\nبا غیرفعال‌سازی، همه خاموش می‌شوند و با فعال‌سازی دوباره فقط همان‌ها روشن می‌شوند."
    done = hold.progress.get("disabled", 0) + hold.progress.get("left", 0)
    lines = [STATUS_TEXT.get(hold.status, hold.status), f"خاموش‌شده با این کار: {done}",
             f"از قبل خاموش (روشن نمی‌شود): {hold.progress.get('skipped', 0)}"]
    if hold.reason:
        lines.append(f"علت: {html.escape(hold.reason)}")
    if not hold.releasable:
        lines.append("این غیرفعال‌سازی را مالک یا فروشگاه دیگری انجام داده است.")
    return "\n".join(lines)


def _panel_markup(kind: str, target: int, panel: HoldPanel, staff: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    hold = panel.hold
    if hold is None:
        rows.append([InlineKeyboardButton(text="⛔ غیرفعال کن", callback_data=f"{PREFIX}:{kind}d:{target}")])
    elif hold.releasable and hold.status in ("applying", "held"):
        rows.append([InlineKeyboardButton(text="✅ فعال‌سازی دوباره", callback_data=f"{PREFIX}:{kind}e:{target}")])
    rows.append([_back(kind, target, staff)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:[{KIND_CUSTOMER}{KIND_SHOP}]:\d+$"))
async def ahb_show(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
                   is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    parsed = _parse(callback.data)
    staff = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    panel = await _panel(session, staff, *parsed) if staff and parsed else None
    if panel is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    kind, target = parsed
    if callback.message:
        await callback.message.edit_text(
            _panel_text(kind, panel), reply_markup=_panel_markup(kind, target, panel, staff)
        )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:[{KIND_CUSTOMER}{KIND_SHOP}]d:\d+$"))
async def ahb_ask_reason(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                         is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    kind, target = callback.data.split(":")[1][0], int(callback.data.split(":")[2])
    staff = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if staff is None or await _panel(session, staff, kind, target) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await state.set_state(AccountHoldStates.reason)
    await state.update_data(ahb_kind=kind, ahb_target=target)
    markup = InlineKeyboardMarkup(inline_keyboard=[[_back(kind, target, staff)]])
    if callback.message:
        await callback.message.edit_text("⛔ علت غیرفعال‌سازی را بفرستید (حداقل ۳ حرف).", reply_markup=markup)


@router.message(AccountHoldStates.reason, F.text)
async def ahb_disable(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
                      is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    data = await state.get_data()
    await state.clear()
    kind, target = str(data.get("ahb_kind") or ""), int(data.get("ahb_target") or 0)
    action = disable_shop if kind == KIND_SHOP else disable_customer
    done = "⛔ غیرفعال شد؛ سرویس‌ها در حال خاموش شدن هستند."
    await _run(message, session, db_user, kind, target, lambda staff: action(session, staff, target, message.text),
               done, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:[{KIND_CUSTOMER}{KIND_SHOP}]e:\d+$"))
async def ahb_enable(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
                     is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    await callback.answer()
    kind, target = callback.data.split(":")[1][0], int(callback.data.split(":")[2])
    action = enable_shop if kind == KIND_SHOP else enable_customer
    done = "✅ در حال فعال‌سازی دوباره؛ آنچه خاموش شده بود روشن می‌شود."
    if callback.message:
        await _run(callback.message, session, db_user, kind, target, lambda staff: action(session, staff, target),
                   done, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)


async def _run(message: Message, session: AsyncSession, db_user: BotUser, kind: str, target: int, call: Any,
               done: str, *, is_reseller_bot: bool, reseller_owner_id: int | None) -> None:
    staff = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[_back(kind, target, staff)]])
    if staff is None or not target:
        await message.answer(feedback.NO_ACCESS)
        return
    try:
        await call(staff)
    except PermissionError:
        await message.answer(feedback.NO_ACCESS)
        return
    except ValueError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=markup)
        return
    except Exception:
        logger.exception("account hold bot action failed kind=%s target=%s", kind, target)
        await rollback_quiet(session)
        await message.answer(FAILED, reply_markup=markup)
        return
    await message.answer(done, reply_markup=markup)
