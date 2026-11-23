"""The «لینک‌های سریع» screen in the bot: the shop's deep links, ready to copy and send to a customer."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.hub_entries import QUICK_LINKS_CALLBACK
from app.bot.nav_inline import with_inline_back
from app.bot.reseller_hub_groups import RESELLER_HOME_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message
from app.services.quick_links import QuickLink, quick_links, shop_bot_username, staff_links_shop
from app.services.users import get_all_settings

router = Router(name="quick_links_view")

# Every shop panel user holds this permission; the links only open public screens of the bot.
BASE_PERMISSION = "dashboard"
ADMIN_HOME_CALLBACK = "nv:adm:home"
TITLE = "🔗 لینک‌های سریع"
HINT = "روی هر لینک بزنید تا کپی شود، سپس برای مشتری بفرستید؛ مشتری با آن مستقیم وارد همان بخش ربات می‌شود."
NO_BOT = "یوزرنیم ربات مشخص نیست؛ اتصال ربات را در وب‌پنل بررسی کنید."
MORE = "لینک تمدید هر سرویس در کارت مشتری و لینک هر کد هدیه در صفحهٔ کدهای هدیهٔ وب‌پنل است."


def quick_links_text(bot_username: str | None, links: list[QuickLink]) -> str:
    """Telegram HTML body: one copyable link per line, or why there are none."""
    if not bot_username:
        return format_message(TITLE, NO_BOT)
    rows = [f"• {html.escape(link.label)}:\n<code>{html.escape(link.url)}</code>" for link in links if link.url]
    lines = [HINT, "", *rows, "", MORE]
    return format_message(TITLE, "\n".join(lines))


@router.callback_query(F.data == QUICK_LINKS_CALLBACK)
async def show_quick_links(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await resolve_feature_staff(
        session, db_user, BASE_PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if staff is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    shop_id = staff_links_shop(staff)
    bot_username = await shop_bot_username(session, shop_id)
    ui = await get_all_settings(session)
    back = ADMIN_HOME_CALLBACK if shop_id is None else RESELLER_HOME_CALLBACK
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=[]), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, quick_links_text(bot_username, quick_links(bot_username)), reply_markup=markup)
