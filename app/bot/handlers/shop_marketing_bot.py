"""Marketing light panels that follow the actor's shop: gift codes and campaigns on the shop bot,
catalog discounts and customer behaviour (funnel) for the platform owner and for shops.

Light operations only, like the owner's gift-code and cashback panels: list, turn a code on or off,
start or pause a campaign, stop a discount (after a confirmation), read the 7-day funnel. Creating
and editing stay in the web panel, linked from every screen. The shop comes from the actor through
``resolve_feature_staff`` and the same service scope rule the web page uses, and every callback
checks it again; the services filter by that shop in SQL, so a forged id of another shop changes
nothing.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.marketing_panels import (
    MARKETING_HUB_CALLBACK,
    SHOP_CAMPAIGN_ACTION_PATTERN,
    SHOP_CAMPAIGN_PANEL_CALLBACK,
    SHOP_GIFT_PANEL_CALLBACK,
    SHOP_GIFT_SET_PATTERN,
    WEB_BUTTON_LABEL,
    campaigns_panel,
    gift_codes_panel,
    panel_page_url,
)
from app.bot.nav_inline import present_nav_panel, with_inline_back
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.shop_marketing_entries import DISCOUNTS_CALLBACK, FUNNEL_CALLBACK
from app.db.models import BotUser, CatalogDiscount
from app.services import bot_feedback_texts as feedback
from app.services.campaigns import CAMPAIGN_LIST_LIMIT, campaign_progress_map, change_campaign_state, list_recent_campaigns
from app.services.catalog_discounts import MODES, SCOPES, stop_discount
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_message, format_number, format_toman
from app.services.funnel_access import funnel_shop_scope
from app.services.gift_codes import describe_code, list_gift_codes, set_gift_code_active
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings
from app.services.ux20 import FUNNEL_STEPS, funnel_summary

logger = logging.getLogger(__name__)
router = Router(name="shop_marketing_bot")

GIFT_PANEL_LIMIT = 8
DISCOUNT_PANEL_LIMIT = 8
FUNNEL_DAYS = 7
DONE = "انجام شد"
GIFT_NOT_FOUND = "کد یافت نشد"
CAMPAIGN_NOT_FOUND = "کمپین یافت نشد یا وضعیت آن تغییر کرده است"
ID = r"(\d{1,9})"
FUNNEL_LABELS: dict[str, str] = {
    "shop_open": "🛍 باز کردن فروشگاه",
    "plan_view": "📦 دیدن پلن",
    "pay_start": "💳 شروع پرداخت",
    "receipt": "🧾 ارسال رسید",
    "delivered": "✅ تحویل",
}


@dataclass(frozen=True, slots=True)
class MarketingActor:
    staff: dict
    shop_id: int | None

    def back(self, shop_action: str) -> str:
        return MARKETING_HUB_CALLBACK if self.shop_id is None else back_callback_for(shop_action)


async def _actor(
    session: AsyncSession, db_user: BotUser, permission: str, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> MarketingActor | None:
    staff = await resolve_feature_staff(
        session, db_user, permission, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not staff:
        return None
    try:
        return MarketingActor(staff=staff, shop_id=resolve_shop_scope_id(staff))
    except ShopScopeError:
        return None


async def _shop_actor(
    session: AsyncSession, db_user: BotUser, permission: str, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> MarketingActor | None:
    """A shop actor only: the owner's own gift-code and campaign panels live in ``marketing_tools``."""
    actor = await _actor(session, db_user, permission, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    return actor if actor is not None and actor.shop_id is not None else None


# ---- gift codes (shop bot) ---------------------------------------------------------------------


async def _show_gift_panel(message: Message, session: AsyncSession, state: FSMContext | None, shop_id: int) -> None:
    ui = await get_all_settings(session)
    codes = await list_gift_codes(session, shop_id=shop_id, limit=GIFT_PANEL_LIMIT)
    details = {code.id: describe_code(code) for code in codes}
    text, markup = gift_codes_panel(
        codes, details, ui=ui, web_url=panel_page_url("/gift-codes"),
        action_prefix="res", back_callback=back_callback_for("res_gift_codes"),
    )
    await present_nav_panel(message, text=text, inline=markup, state=state)


async def open_shop_gift_panel(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    """Shop hub leaf (callback or reply navigation)."""
    actor = await _shop_actor(session, db_user, "gift_codes", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await message.answer(feedback.NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await _show_gift_panel(message, session, state, int(actor.shop_id))


@router.callback_query(F.data == SHOP_GIFT_PANEL_CALLBACK)
async def open_shop_gift_cb(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, "gift_codes", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await state.clear()
        await _show_gift_panel(callback.message, session, state, int(actor.shop_id))


@router.callback_query(F.data.regexp(SHOP_GIFT_SET_PATTERN.pattern))
async def set_shop_gift_state(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, "gift_codes", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    match = SHOP_GIFT_SET_PATTERN.match(callback.data or "")
    if actor is None or match is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    changed = await set_gift_code_active(
        session, int(match.group(1)), shop_id=int(actor.shop_id), active=match.group(2) == "1"
    )
    await callback.answer(DONE if changed else GIFT_NOT_FOUND, show_alert=not changed)
    if callback.message is not None:
        await _show_gift_panel(callback.message, session, state, int(actor.shop_id))


# ---- campaigns (shop bot) ----------------------------------------------------------------------


async def _show_campaign_panel(message: Message, session: AsyncSession, state: FSMContext | None, shop_id: int) -> None:
    ui = await get_all_settings(session)
    campaigns = await list_recent_campaigns(session, shop_id=shop_id, limit=CAMPAIGN_LIST_LIMIT)
    progress = await campaign_progress_map(session, [campaign.id for campaign in campaigns])
    text, markup = campaigns_panel(
        campaigns, progress, ui=ui, web_url=panel_page_url("/campaigns"),
        action_prefix="res", back_callback=back_callback_for("res_campaigns"),
    )
    await present_nav_panel(message, text=text, inline=markup, state=state)


async def open_shop_campaign_panel(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    """Shop hub leaf (callback or reply navigation)."""
    actor = await _shop_actor(session, db_user, "campaigns", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await message.answer(feedback.NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await _show_campaign_panel(message, session, state, int(actor.shop_id))


@router.callback_query(F.data == SHOP_CAMPAIGN_PANEL_CALLBACK)
async def open_shop_campaign_cb(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, "campaigns", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await state.clear()
        await _show_campaign_panel(callback.message, session, state, int(actor.shop_id))


@router.callback_query(F.data.regexp(SHOP_CAMPAIGN_ACTION_PATTERN.pattern))
async def change_shop_campaign(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, "campaigns", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    match = SHOP_CAMPAIGN_ACTION_PATTERN.match(callback.data or "")
    if actor is None or match is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        await change_campaign_state(session, int(match.group(2)), shop_id=int(actor.shop_id), action=match.group(1))
    except ValueError:
        await rollback_quiet(session)
        await callback.answer(CAMPAIGN_NOT_FOUND, show_alert=True)
    else:
        await callback.answer(DONE)
    if callback.message is not None:
        await _show_campaign_panel(callback.message, session, state, int(actor.shop_id))


# ---- catalog discounts (owner and shops) -------------------------------------------------------


def _reduction_text(row: CatalogDiscount) -> str:
    if row.reduction_kind == "percent":
        return f"{format_number(int(row.reduction_value))}٪"
    return format_toman(int(row.reduction_value))


def _discount_line(row: CatalogDiscount) -> str:
    ends = f" — تا <code>{row.ends_at.strftime('%Y-%m-%d %H:%M')}</code>" if row.ends_at else ""
    return (
        f"• <b>{html.escape(row.title)}</b> — {html.escape(MODES.get(row.mode, row.mode))} "
        f"{_reduction_text(row)} — {html.escape(SCOPES.get(row.scope, row.scope))}{ends}"
    )


async def _active_discounts(session: AsyncSession, shop_id: int | None) -> list[CatalogDiscount]:
    query = (
        select(CatalogDiscount)
        .where(
            CatalogDiscount.reseller_id.is_(None) if shop_id is None else CatalogDiscount.reseller_id == int(shop_id),
            CatalogDiscount.shop_key == (shop_id or 0),
            CatalogDiscount.is_active.is_(True),
        )
        .order_by(CatalogDiscount.id.desc())
        .limit(DISCOUNT_PANEL_LIMIT)
    )
    return list((await session.scalars(query)).all())


def _web_rows(path: str) -> list[list[InlineKeyboardButton]]:
    url = panel_page_url(path)
    return [[InlineKeyboardButton(text=WEB_BUTTON_LABEL, url=url)]] if url else []


async def _present(message: Message | None, session: AsyncSession, text: str, rows: list, back: str) -> None:
    if message is None:
        return
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    await present_nav_panel(message, text=text, inline=markup, state=None)


async def _render_discounts(message: Message | None, session: AsyncSession, actor: MarketingActor) -> None:
    rows_db = await _active_discounts(session, actor.shop_id)
    if rows_db:
        body = "تخفیف‌ها و کاهش قیمت‌های فعال؛ ساخت و ویرایش از وب‌پنل انجام می‌شود.\n\n" + "\n".join(
            _discount_line(row) for row in rows_db
        )
    else:
        body = "تخفیف فعالی نیست. ساخت تخفیف از وب‌پنل انجام می‌شود."
    rows = [
        [InlineKeyboardButton(text=f"⏹ توقف: {row.title}"[:64], callback_data=f"dsc:s:{int(row.id)}")]
        for row in rows_db
    ]
    rows += _web_rows("/catalog-discounts")
    await _present(message, session, format_message("🏷 تخفیف‌ها", body), rows, actor.back("res_discounts"))


async def _discount_actor(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> MarketingActor | None:
    actor = await _actor(session, db_user, "catalog_discounts", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
    return actor


@router.callback_query(F.data == DISCOUNTS_CALLBACK)
async def show_discounts(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _discount_actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    await callback.answer()
    await _render_discounts(callback.message, session, actor)


async def open_discount_panel(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    """Shop hub leaf from reply navigation."""
    actor = await _actor(session, db_user, "catalog_discounts", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await message.answer(feedback.NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await _render_discounts(message, session, actor)


@router.callback_query(F.data.regexp(rf"^dsc:s:{ID}$"))
async def confirm_stop_discount(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _discount_actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    discount_id = int(callback.data.split(":")[2])
    row = next((r for r in await _active_discounts(session, actor.shop_id) if int(r.id) == discount_id), None)
    if row is None:
        await callback.answer("تخفیف یافت نشد", show_alert=True)
        return
    await callback.answer()
    text = format_message(
        "⚠️ توقف تخفیف",
        f"{_discount_line(row)}\n\nقیمت‌ها به حالت قبل برمی‌گردند و اطلاع‌رسانی صف‌شدهٔ آن متوقف می‌شود.",
    )
    rows = [[InlineKeyboardButton(text="⏹ بله، متوقف شود", callback_data=f"dsc:S:{discount_id}")]]
    await _present(callback.message, session, text, rows, DISCOUNTS_CALLBACK)


@router.callback_query(F.data.regexp(rf"^dsc:S:{ID}$"))
async def run_stop_discount(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _discount_actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    try:
        await stop_discount(session, actor.staff, int(callback.data.split(":")[2]))
    except (ValueError, PermissionError) as exc:
        await rollback_quiet(session)
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer("تخفیف متوقف شد ✅", show_alert=True)
    await _render_discounts(callback.message, session, actor)


# ---- customer behaviour (owner and shops) ------------------------------------------------------


async def _funnel_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> MarketingActor | None:
    """Same rule as the web page: orders or payments, and the actor's own shop."""
    for permission in ("orders", "payments"):
        actor = await _actor(session, db_user, permission, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
        if actor is None:
            continue
        try:
            return MarketingActor(staff=actor.staff, shop_id=funnel_shop_scope(actor.staff))
        except PermissionError:
            continue
    return None


def funnel_text(counts: dict[str, int]) -> str:
    """Step counts with each step's share of the shop opens."""
    first = max(int(counts.get(FUNNEL_STEPS[0], 0)), 0)
    lines = []
    for step in FUNNEL_STEPS:
        value = max(int(counts.get(step, 0)), 0)
        share = f" ({format_number(round(value * 100 / first))}٪)" if first and step != FUNNEL_STEPS[0] else ""
        lines.append(f"{FUNNEL_LABELS[step]}: <b>{format_number(value)}</b>{share}")
    return format_message("📊 رفتار کاربر", f"{format_number(FUNNEL_DAYS)} روز اخیر — از باز کردن فروشگاه تا تحویل\n\n" + "\n".join(lines))


async def _render_funnel(message: Message | None, session: AsyncSession, actor: MarketingActor) -> None:
    try:
        counts = await funnel_summary(session, reseller_id=actor.shop_id, days=FUNNEL_DAYS)
    except SQLAlchemyError:
        await rollback_quiet(session)
        logger.warning("bot funnel summary failed shop=%s", actor.shop_id, exc_info=True)
        counts = {}
    await _present(message, session, funnel_text(counts), _web_rows("/funnel"), actor.back("res_funnel"))


@router.callback_query(F.data == FUNNEL_CALLBACK)
async def show_funnel(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _funnel_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await _render_funnel(callback.message, session, actor)


async def open_funnel_panel(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    """Shop hub leaf from reply navigation."""
    actor = await _funnel_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await message.answer(feedback.NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await _render_funnel(message, session, actor)


# Shop hub actions opened from reply navigation (``reply_nav._soft_reseller``).
SHOP_LEAF_OPENERS = {
    "res_gift_codes": open_shop_gift_panel,
    "res_campaigns": open_shop_campaign_panel,
    "res_discounts": open_discount_panel,
    "res_funnel": open_funnel_panel,
}
