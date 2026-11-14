"""Read-only staff action log in the bot: the platform Owner reads the platform's log, a shop owner the shop's."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_number
from app.services.shop_scope import is_platform_admin
from app.services.staff_audit import list_staff_actions, staff_action_views, staff_audit_scope
from app.services.users import get_all_settings

router = Router(name="staff_audit_view")

# Every shop has this permission; the log itself is limited to the shop owner below.
BASE_PERMISSION = "dashboard"
PAGE_SIZE = 8
PLATFORM_BACK = "nv:adm:system"
SHOP_BACK = "nv:res:home"
REASON_PREVIEW = 120


async def audit_shop_scope(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> tuple[bool, int | None, dict | None]:
    """(allowed, shop_id, staff). A shop's bot admins never read the log; only the shop owner does."""
    staff = await resolve_feature_staff(
        session, db_user, BASE_PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if staff is None:
        return False, None, None
    try:
        shop_id = staff_audit_scope(staff)
    except PermissionError:
        return False, None, None
    if shop_id is not None and int(db_user.id) != int(shop_id):
        return False, None, None
    return True, shop_id, staff


@router.callback_query(F.data.regexp(r"^saud:l:(\d{1,12})$"))
async def show_staff_actions(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    allowed, shop_id, staff = await audit_shop_scope(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not allowed:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    before = int(callback.data.split(":")[2]) or None
    rows = await list_staff_actions(session, shop_id=shop_id, before_id=before, limit=PAGE_SIZE)
    views = staff_action_views(rows)
    lines = []
    for view in views:
        amount = f" · {format_number(view.amount)}" if view.amount is not None else ""
        reason = f"\n   ↳ {html.escape(view.reason[:REASON_PREVIEW])}" if view.reason else ""
        lines.append(
            f"• {html.escape(view.when)} — <b>{html.escape(view.action)}</b> · {html.escape(view.target)}{amount}\n"
            f"   {html.escape(view.actor_kind)}: {html.escape(view.actor)}{reason}"
        )
    body = "\n\n".join(lines) or "هنوز اقدامی ثبت نشده است."
    nav = []
    if len(rows) == PAGE_SIZE:
        nav.append([InlineKeyboardButton(text="قدیمی‌تر ▶️", callback_data=f"saud:l:{rows[-1].id}")])
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=nav), ui, PLATFORM_BACK if is_platform_admin(staff) else SHOP_BACK)
    if callback.message:
        await safe_edit_text(callback.message, format_message("🧾 گزارش اقدامات کارکنان", body), reply_markup=markup)
