"""Level-1 shop: light panel for child PAYG price plans (create/edit stays in the web panel).

Opened from the customers hub the same way shop broadcast is: dedicated callback,
``state.clear()``, then ``present_nav_panel`` with the group Back button.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import _ikb, back_button
from app.bot.marketing_panels import WEB_BUTTON_LABEL, panel_page_url
from app.bot.nav_inline import present_nav_panel
from app.bot.reseller_hub_groups import back_callback_for
from app.config import get_settings
from app.db.models import BotUser, ChildPricePlan
from app.services.child_pricing import list_child_plans
from app.services.formatting import format_toman
from app.services.representative_unification import shop_bot_can_manage_representatives
from app.services.reseller_access import load_reseller_actor
from app.services.shop_credit import credit_account, effective_limit, reserved_for_children
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="child_pricing_bot")

PANEL_CALLBACK = "res:cp"
NO_ACCESS = "مدیریت قیمت زیرمجموعه برای این حساب فعال نیست"


async def _parent_shop_id(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> int | None:
    if not await shop_bot_can_manage_representatives(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    ):
        return None
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=True,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        return None
    return int(owner_id)


def _plan_line(plan: ChildPricePlan, *, currency: str) -> str:
    name = html.escape(plan.name or f"#{plan.id}")
    gb = format_toman(int(plan.price_per_gb or 0), currency)
    user = format_toman(int(plan.payg_user_price or 0), currency)
    return f"#{plan.id} — {name} — گیگ {gb} — یوزر {user}"


def _panel_markup(ui: dict, *, web_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if web_url:
        rows.append([_ikb(WEB_BUTTON_LABEL, url=web_url, ui=ui)])
    rows.append([back_button(ui, back_callback_for("res_child_pricing"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_child_pricing_panel(
    message: Message,
    session: AsyncSession,
    state: FSMContext | None,
    *,
    parent_shop_id: int,
) -> None:
    ui = await get_all_settings(session)
    currency = get_settings().currency
    plans = await list_child_plans(session, parent_shop_id)
    now = datetime.now(timezone.utc)
    own = effective_limit(await credit_account(session, parent_shop_id), now=now)
    reserved = await reserved_for_children(session, parent_shop_id, now=now)
    free = max(0, own - reserved)
    lines = [
        "🏷 <b>قیمت و اعتبار زیرمجموعه</b>",
        f"اعتبار آزاد شما: <b>{format_toman(free, currency)}</b>",
        "",
        "پلن‌های قیمت PAYG برای نمایندگان سطح ۲. ساخت و انتساب از وب‌پنل است.",
        "",
    ]
    if plans:
        lines.extend(_plan_line(plan, currency=currency) for plan in plans)
    else:
        lines.append("هنوز پلن قیمتی ساخته نشده.")
    await present_nav_panel(
        message,
        text="\n".join(lines),
        inline=_panel_markup(ui, web_url=panel_page_url("/resellers/children-pricing")),
        state=state,
    )


async def open_child_pricing_panel(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    push: bool = True,
) -> None:
    _ = push
    parent_shop_id = await _parent_shop_id(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if parent_shop_id is None:
        await message.answer(NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await show_child_pricing_panel(message, session, state, parent_shop_id=parent_shop_id)


@router.callback_query(F.data == PANEL_CALLBACK)
async def open_child_pricing_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    parent_shop_id = await _parent_shop_id(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if parent_shop_id is None:
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message is not None:
        await show_child_pricing_panel(
            callback.message, session, state, parent_shop_id=parent_shop_id
        )
